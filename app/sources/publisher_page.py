"""出版商落地页与图床直连：ScienceDirect / IEEE Xplore / Springer 的图表兜底通道。

为什么必须单开一条通道（实测结论，2026-11）：
  · `sciencedirect.com/science/article/pii/…` 对非订阅 IP 一律 403 验证码/挑战页，
    body 1.2MB 里没有正文、没有 `<table>`，也没有图床地址；headless Chrome 拿到的
    仍是 challenge 页；`/sdfe/arp/…` 内部接口、`/pdfft` 全部 403；
  · 但 Elsevier 图床 `ars.els-cdn.com` 是静态 CDN，**不校验来源页**：
    `1-s2.0-{PII}-gr{n}_lrg.jpg` 直接 200（469KB 级别），并且支持 HEAD 探测；
    不存在的图返回 **404 + text/xml 189B**，所以判定存在性必须同时看状态码与
    content-type，只看状态码会把 404 当成功；
  · 于是「正文页被墙」不等于「图表拿不到」——这就是 PR 2026 WeaCount 成稿里
    既没有模型结构图也没有 Table 4 的根因。

本模块做三件事：
1. 从任意 ScienceDirect / linkinghub 链接抠 PII，HEAD 探测 gr/ga 大图，按已知图注
   （来自 OA XML / PDF）挑架构图；图注未知时取 Fig. 1，但图注写中性文案——
   没看到图注就不谎称「架构图」；
2. 落地页能打开时（有 cookie / 开放出版商），用专门的 `<table>` 解析器把正文表格
   无损转成 `{caption, header, rows}`：rowspan/colspan 展平成网格、两行表头合并、
   单元格与表注清洗后过合理性校验——宁可整表丢弃，也不把版式表当实验数据；
3. 落地页被反爬拦下时只在日志里说明一次，**绝不重试轰炸**：对验证码页重试只会
   换来更长的封禁，而图床仍可直连，重试没有收益。
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from .base import HttpClient, abs_url

# PII 是 Elsevier 的文档主键：S + 15 位数字（S0031320326021515 这种形态）
_PII_RE = re.compile(r"/pii/(s\d{9,17})(?![\d])", re.I)
_PII_QUERY_RE = re.compile(r"[?&]pii=(s\d{9,17})(?![\d])", re.I)
# 图床小图 → 大图：-gr3.jpg / -gr3.sml / -gr3__inline.jpg 统一升级成 -gr3_lrg.jpg
_LRG_RE = re.compile(r"-(gr|ga)(\d{1,2})(?:__[a-z0-9]+)?\.(?:jpe?g|png|gif|webp|sml)$", re.I)
_LRG_DONE_RE = re.compile(r"_(?:lrg|large)\.(?:jpe?g|png)$", re.I)
_ELSEVIER_CDN = "https://ars.els-cdn.com/content/image/1-s2.0-{pii}-{kind}{n}_lrg.jpg"
# 图注里的图号：Figure 3 / Fig. 3 / 图 3
_FIG_NUM_RE = re.compile(r"(?:figure|fig\.?|图)\s*(\d{1,2})", re.I)
# DOI：检索源给的落地页常常是 https://doi.org/10.1016/j.patcog.2026.115188
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#\"'<>]+")
# 指标词：只用来判断「这张表是不是实验结果表」，不用来改数字
_METRIC_RE = re.compile(
    r"(?<![a-z0-9])(mae|mse|rmse|psnr|ssim|lpips|f1|precision|recall|map|miou|iou|"
    r"accuracy|acc|auc|bleu|meteor|fps|params?|flops|gflops)(?![a-z0-9])",
    re.I,
)
# 允许的数值单元：12.3 / 93.4% / 12.3 ± 0.4 / 93.4/91.2 / 1,234 / 0.5*
_NUM_LOOSE_RE = re.compile(
    r"^[-+±<>≤≥~]?\s*\d[\d,]*(?:\.\d+)?\s*(?:%|‰)?"
    r"(?:\s*(?:/|±|–|-|~)\s*[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|‰)?)*"
    r"\**[†‡]?$",
    re.I,
)
# 表格单元格长度上限：超过它就是正文段落，说明抓到的是版式表
_MAX_CELL_CHARS = 80
_MIN_FIGURE_BYTES = 5 * 1024


# ---------------------------------------------------------------- 链接与图床
def elsevier_pii(url: str) -> str:
    """从 ScienceDirect / linkinghub / pdfft 链接里抠 PII（统一大写）。"""
    text = str(url or "")
    m = _PII_RE.search(text) or _PII_QUERY_RE.search(text)
    return m.group(1).upper() if m else ""


def _lrg(url: str) -> str:
    """把小图地址升级成期刊图床的大图（Large Image）。

    Elsevier 的正文图有三个变体：`-gr3.jpg`（页面缩略）、`-gr3.sml`（最小）、
    `-gr3_lrg.jpg`（出版级大图，既清晰又不超微信 8MB 上限）。IEEE Xplore
    mediastore / Springer 的地址本身就是原图，没有 `_lrg` 变体时原样返回。
    """
    if not url:
        return url
    if _LRG_DONE_RE.search(url.split("?")[0]):
        return url
    path = url.split("?")[0]
    m = _LRG_RE.search(path)
    if not m:
        return url
    return f"{path[:m.start()]}-{m.group(1)}{m.group(2)}_lrg.jpg"


def _cdn_image(pii: str, kind: str, n: int) -> str:
    return _ELSEVIER_CDN.format(pii=pii, kind=kind, n=n)


def _referer(pii: str) -> str:
    return f"https://www.sciencedirect.com/science/article/pii/{pii}"


async def resolve_pii(client: HttpClient, landing_url: str, *, log=print) -> str:
    """落地页 URL / DOI → Elsevier PII，供图床拼 `_lrg.jpg` 地址。

    检索源给的往往不是 ScienceDirect 链接而是 `https://doi.org/{DOI}`
    （Crossref/OpenAlex 都是这么给的），里面没有 PII——但 Elsevier 的 DOI
    解析是 302 到 `linkinghub.elsevier.com/retrieve/pii/{PII}`，**Location 头里
    就带着 PII**，不用读正文，也就绕开了正文页的验证码墙。只有拿不到
    Location 时才跟随一次重定向取最终 URL 兜底。
    """
    pii = elsevier_pii(landing_url)
    if pii:
        return pii
    m = _DOI_RE.search(str(landing_url or ""))
    if not m:
        return ""
    doi_url = f"https://doi.org/{m.group(0)}"
    try:
        resp = await client._client.get(doi_url, follow_redirects=False, timeout=20.0)
        pii = (elsevier_pii(str(resp.headers.get("location") or ""))
               or elsevier_pii(str(getattr(resp, "url", "") or "")))
        if pii:
            return pii
        resp = await client._client.get(doi_url, follow_redirects=True, timeout=20.0)
        pii = elsevier_pii(str(getattr(resp, "url", "") or ""))
    except Exception as exc:
        log(f"素材兜底：DOI 解析 PII 失败（{type(exc).__name__}），图床通道跳过")
        return ""
    if not pii:
        log("素材兜底：该 DOI 不是 Elsevier 文献，图床通道跳过")
    return pii


async def _head_size(client: HttpClient, url: str) -> int:
    """HEAD 探测图床大图：-1 = 明确没有，>=0 = 存在（0 表示没给 content-length）。

    不走 `HttpClient._request`：那里把 status>=400 包成 SourceError 并做三次指数
    退避重试——对「这张图本来就不存在」的 404 重试纯属浪费，还会拖慢流水线；
    这里也绝不读 body，省掉几百 KB 的无效流量。
    """
    try:
        resp = await client._client.head(url, timeout=12.0)
    except Exception:
        return 0          # 网络抖动不等于图不存在，交给下载环节实测
    if resp.status_code >= 400:
        return -1
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if not ctype.startswith("image/"):
        return -1
    try:
        size = int(resp.headers.get("content-length") or 0)
    except ValueError:
        size = 0
    if size and size < _MIN_FIGURE_BYTES:
        return -1
    return size


async def _fetch_figure(client: HttpClient, url: str, referer: str,
                        caption: str, role: str) -> tuple[str, str, str] | None:
    """下载单张图；体积过小（占位图/客服图标）视为失败，返回 None。"""
    try:
        path = await asyncio.wait_for(
            client.download_image(url, referer=referer, timeout=25.0), timeout=40)
    except Exception:
        return None
    if not path:
        return None
    try:
        if Path(path).stat().st_size < _MIN_FIGURE_BYTES:
            return None
    except OSError:
        return None
    return (path, caption, role)


async def elsevier_cdn_figures(client: HttpClient, pii: str, *, captions=None,
                               limit: int = 4, max_probe: int = 8,
                               log=print) -> list[tuple[str, str, str]]:
    """枚举 Elsevier 图床的 gr/ga 大图并下载，返回 [(本地路径, 图注, 角色)]。

    有图注（OA XML / PDF 抽到的 fig_captions）时按「架构 → teaser → 结果 → 其它」
    排序挑图，架构图才能真正落到「核心创新」节；图注未知时只取 Fig. 1，
    且用中性文案兜底——用户看到的是真实图，我们不做无依据的图注推断。
    """
    from .paper_pdf import _figure_role   # 延迟导入：paper_pdf 调用本模块，避免循环

    if not pii:
        return []
    known = {}
    for key, value in (captions or {}).items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        text = str(value or "").strip()
        if number and text:
            known[number] = text

    probes: list[tuple[str, int]] = [("ga", 1)] + [("gr", n) for n in range(1, max_probe + 1)]
    urls = [_cdn_image(pii, kind, n) for kind, n in probes]
    sizes = await asyncio.gather(*[_head_size(client, u) for u in urls])
    alive = [(kind, n, u) for (kind, n), u, size in zip(probes, urls, sizes) if size >= 0]
    if not alive:
        log(f"素材兜底：Elsevier 图床无可用大图（PII {pii}）")
        return []

    picked: list[tuple[str, str, str]] = []      # (url, caption, role)
    named = [(kind, n, u) for kind, n, u in alive if kind == "gr" and n in known]
    if named:
        rank = {"arch": 0, "teaser": 1, "result": 2, "other": 3}
        named.sort(key=lambda it: (rank.get(_figure_role(known[it[1]], it[1]), 3), it[1]))
        for _kind, n, url in named[:limit]:
            caption = known[n]
            picked.append((url, caption, _figure_role(caption, n)))
    else:
        graphic = next((it for it in alive if it[0] == "ga"), None)
        first = next((it for it in alive if it[0] == "gr" and it[1] == 1), None)
        if graphic:
            picked.append((graphic[2], "图形摘要（Graphical Abstract）", "arch"))
        if first:
            if 1 in known:
                caption = known[1]
                picked.append((first[2], caption, _figure_role(caption, 1)))
            else:
                # 图注未知：图是真的，就不编图注——中性文案，不谎称「架构图」
                picked.append((first[2], "Fig. 1 论文原图（出版商图床高清大图）", "arch"))

    referer = _referer(pii)
    out: list[tuple[str, str, str]] = []
    for url, caption, role in picked:
        fig = await _fetch_figure(client, url, referer, caption, role)
        if fig:
            out.append(fig)
    if out:
        log(f"素材兜底：出版商图床取到 {len(out)} 张大图（{pii}）")
    else:
        log(f"素材兜底：Elsevier 图床探测到 {len(alive)} 张大图但下载失败（{pii}）")
    return out


# ---------------------------------------------------------------- 落地页解析
def _flatten(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _clean_cell(text: str) -> str:
    """单元格清洗：去文献引用、压空白、统一减号与全角空格。"""
    text = re.sub(r"\[\s*\d+(?:\s*[,–-]\s*\d+)*\s*\]", "", str(text or ""))
    text = text.replace("\u2212", "-").replace("\u00a0", " ")
    return re.sub(r"[\s\u3000]+", " ", text).strip()


def _span(cell) -> tuple[int, int]:
    try:
        rowspan = max(1, int(str(cell.get("rowspan") or "1")))
        colspan = max(1, int(str(cell.get("colspan") or "1")))
    except ValueError:
        rowspan = colspan = 1
    return min(rowspan, 40), min(colspan, 12)


def _grid_from_table(tbl) -> list[list[str]]:
    """`<table>` → 平铺网格，rowspan/colspan 按落位表展开（与 JATS 解析同款算法）。"""
    grid: list[list[str]] = []
    pending: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(tbl.find_all("tr")):
        cells = tr.find_all(["td", "th"], recursive=False)
        if not cells:
            continue
        row: list[str] = []
        c = 0
        for cell in cells:
            while (r, c) in pending:
                row.append(pending.pop((r, c)))
                c += 1
            rowspan, colspan = _span(cell)
            text = _clean_cell(cell.get_text(" ", strip=True))
            for rr in range(rowspan):
                for cc in range(colspan):
                    if rr == 0:
                        row.append(text)
                    else:
                        pending[(r + rr, c + cc)] = text
            c += colspan
        while (r, c) in pending:
            row.append(pending.pop((r, c)))
            c += 1
        if any(v for v in row):
            grid.append(row)
    return grid


def _table_caption(tbl) -> str:
    """表注：`<caption>` → 父级 `.captions/.label` → 前一个含「Table N」的兄弟节点。"""
    node = tbl.find("caption")
    if node is not None:
        text = _flatten(node.get_text(" ", strip=True))
        if text:
            return text
    parent = tbl.parent
    for _ in range(4):
        if parent is None:
            break
        found = parent.select_one(".captions, .caption, figcaption, .table-caption, .label")
        if found is not None:
            text = _flatten(found.get_text(" ", strip=True))
            if text:
                return text
        parent = parent.parent
    sibling = tbl.find_previous(
        lambda tag: tag.name in ("div", "p", "span", "h2", "h3", "h4", "strong")
        and bool(re.match(r"^table\s*\d", _flatten(tag.get_text(" ", strip=True)), re.I))
        and len(_flatten(tag.get_text(" ", strip=True))) < 200)
    return _flatten(sibling.get_text(" ", strip=True)) if sibling is not None else ""


def _plausible_html_grid(grid: list[list[str]]) -> bool:
    """HTML 表合理性：至少 2 行 2 列、无正文长句、数字单元占比达标。

    HTML 表不像 PDF 那样会把「84.09」拆行，主要风险是抓到导航表/版式表，
    所以用「长句 = 版式表」+「数字占比」两条判据，宁可少收也不污染成稿。
    """
    if len(grid) < 2:
        return False
    if max(len(r) for r in grid) < 2:
        return False
    cells = [c for row in grid for c in row if c]
    if not cells or any(len(c) > _MAX_CELL_CHARS for c in cells):
        return False
    numeric = sum(1 for c in cells if _NUM_LOOSE_RE.match(c))
    return numeric >= max(2, int(len(cells) * 0.15))


def tables_from_html(html: str, limit: int = 3) -> list[dict]:
    """落地页 `<table>` → `[{caption, header, rows}]`（Markdown 渲染层直接可用）。

    只收「像实验数据」的表：展平网格后至少 2 行 2 列、数字单元占比 >= 15%，
    两行表头（分组表头 + 子表头）合并成 `R@0.5` 这类单行列名，否则模型会把
    分组行当数据行，整张表失真。文档顺序即论文顺序（Table 1/2/4 依次保留）。
    """
    from bs4 import BeautifulSoup

    from .paper_pdf import _is_two_row_header   # 延迟导入，避免与 paper_pdf 循环

    if not html or "<table" not in html.lower():
        return []
    soup = BeautifulSoup(html, "lxml")
    out: list[dict] = []
    for tbl in soup.find_all("table"):
        if len(out) >= limit:
            break
        grid = _grid_from_table(tbl)
        if not _plausible_html_grid(grid):
            continue
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid]
        if _is_two_row_header(grid):
            header = [grid[0][j] if grid[0][j] == grid[1][j] or not grid[0][j]
                      else (f"{grid[1][j]}@{grid[0][j]}" if _NUM_LOOSE_RE.match(grid[0][j])
                            else f"{grid[1][j]}({grid[0][j]})")
                      for j in range(width)]
            rows = grid[2:]
        else:
            header, rows = grid[0], grid[1:]
        if not any(h for h in header):
            continue
        out.append({
            "caption": _table_caption(tbl)[:200],
            "header": [h[:24] for h in header],
            "rows": [[c[:24] for c in r] for r in rows[:12]],
        })
    return out


def _best_src(img) -> str:
    """取图片真实地址：srcset 里最大的那个 > data-src（懒加载）> src。"""
    srcset = str(img.get("srcset") or "")
    if srcset:
        parts = [p.strip().split(" ")[0] for p in srcset.split(",") if p.strip()]
        if parts:
            return parts[-1]
    for attr in ("data-src", "data-original", "src"):
        value = str(img.get(attr) or "").strip()
        if value and not value.startswith("data:"):
            return value
    return ""


def figures_from_html(html: str, base_url: str) -> tuple[list[tuple[str, str, str]],
                                                          dict[int, str]]:
    """落地页插图 → ([(绝对地址, 图注, 角色)], {图号: 图注})。

    地址统一过 `_lrg` 升级成图床大图；图注取 `<figcaption>`（ScienceDirect 的
    `.captions` 也算）。返回的是 URL 不是本地文件，方便上层决定要不要下载。
    """
    from bs4 import BeautifulSoup

    from .paper_pdf import _figure_role

    if not html:
        return [], {}
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.find_all("figure")
    if not nodes:
        nodes = soup.select("div.figure, div[class*=figure], section[class*=figure]")
    figures: list[tuple[str, str, str]] = []
    captions: dict[int, str] = {}
    seen: set[str] = set()
    for node in nodes:
        img = node.find("img")
        if img is None:
            continue
        url = _lrg(abs_url(base_url, _best_src(img)))
        if not url or url in seen:
            continue
        caption = ""
        cap_node = node.find("figcaption") or node.select_one(".captions, .caption")
        if cap_node is not None:
            caption = _flatten(cap_node.get_text(" ", strip=True))[:220]
        m = _FIG_NUM_RE.search(caption)
        number = int(m.group(1)) if m else 0
        if number and caption:
            captions[number] = caption
        seen.add(url)
        figures.append((url, caption or f"论文原图 Fig. {number or len(figures) + 1}",
                        _figure_role(caption, number)))
    return figures, captions


async def _page_html(client: HttpClient, url: str, *, log=print) -> str:
    """取落地页 HTML；被反爬拦下返回空串（只记一条日志，不重试）。"""
    if not str(url or "").startswith("http"):
        return ""
    headers: dict[str, str] = {}
    cookie = (os.getenv("SCIENCEDIRECT_COOKIE") or os.getenv("ELSEVIER_COOKIE") or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    try:
        resp = await client._client.get(url, headers=headers, timeout=25.0)
    except Exception as exc:
        log(f"素材兜底：出版商页面抓取失败（{type(exc).__name__}），改走图床/备用全文通道")
        return ""
    if resp.status_code >= 400:
        log(f"素材兜底：出版商页面被拦（HTTP {resp.status_code}），改走图床/备用全文通道")
        return ""
    html = resp.text or ""
    low = html.lower()
    if "<table" not in low and "<figure" not in low and "figcaption" not in low:
        log("素材兜底：出版商页面返回验证码/挑战页，改走图床/备用全文通道")
        return ""
    return html


async def publisher_assets(client: HttpClient, landing_url: str, *, captions=None,
                           limit: int = 3, log=print) -> dict:
    """落地页 + 图床合流，返回 {tables, fig_captions, figures, blocked}。

    `captions` 是 OA XML / PDF 已经抽到的图注（{图号: 图注}），比落地页更干净，
    优先采用——图注决定「哪张图是架构图」，不能靠猜。
    """
    out: dict = {"tables": [], "fig_captions": {}, "figures": [], "blocked": False}
    pii = await resolve_pii(client, landing_url, log=log)
    html = await _page_html(client, landing_url, log=log)
    out["blocked"] = not html
    page_figures: list[tuple[str, str, str]] = []
    caps: dict[int, str] = {}
    if html:
        out["tables"] = tables_from_html(html, limit=limit)
        page_figures, caps = figures_from_html(html, landing_url)
    for key, value in (captions or {}).items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            continue
        text = str(value or "").strip()
        if number and text:
            caps[number] = text
    out["fig_captions"] = caps

    if pii:
        out["figures"] = await elsevier_cdn_figures(
            client, pii, captions=caps, limit=min(4, limit + 1), log=log)
    else:
        for url, caption, role in page_figures[:2]:
            fig = await _fetch_figure(client, url, landing_url, caption, role)
            if fig:
                out["figures"].append(fig)
    return out
