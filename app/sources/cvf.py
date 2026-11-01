"""CVF Open Access · NTIRE 官方挑战赛报告源（赛事 baseline 精读的「权威底座」）。

为什么单独建一个源：
Datawhale 的 baseline 仓库给的是**代码起点**，而 NTIRE 每年的 challenge report 给的是
**赛道定义 + 数据规格 + 评测口径 + 完整榜单 + 各队方法描述**。后者是这条管线里信息密度最高的
素材——一篇报告本身就能撑起「数据说明 / 评估指标 / 奖项 / 解题思路」四节，而且全部可溯源。

链路分三段：
1. 发现：`openaccess.thecvf.com/menu` → 近 N 届 workshop 入口 → 该届的 NTIRE 页；
2. 建条：NTIRE 页里标题含「NTIRE 20XX Challenge」的都是官方挑战赛报告，一条一份；
3. 深挖：`enrich_report()` 抓摘要页 + 报告 PDF 前 1.2MB（Range 分段）→ PyMuPDF 抽正文 →
   解析方法小节 / 队伍 / 参赛规模 / 榜单要点 → 拿到冠军方案后，再去 CSDN 与知乎定向检索
   冠军选手的复盘与解读。

PDF 为什么要分段下：整本 2–3MB，慢网络下要三分钟；前 1.2MB 已覆盖摘要、赛道定义、
榜单表与全部方法小节（实测 600KB ≈ 15 页），剩下的是参考文献。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..config import settings, topics
from ..models import Item
from .base import HttpClient, abs_url, strip_html

try:  # 解析 CVF 页面用；缺依赖时本源自动降级为空
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore

CVF_BASE = "https://openaccess.thecvf.com"
MENU_URL = f"{CVF_BASE}/menu"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 底层视觉赛道词：命中的报告优先入选精读——NTIRE 也有不少检测/工业类赛道，
# 但栏目读者最认的是复原类（超分、去噪、低光、RAW、HDR…）
_LOWLEVEL_WORDS = (
    "super-resolution", "super resolution", "denoising", "deblurring", "deblur",
    "low-light", "low light", "night", "raw", "hdr", "restoration", "restore",
    "compression", "inpainting", "depth", "quality assessment", "enhancement",
    "demosaic", "flare", "shadow", "reflection", "bokeh", "frame interpolation",
    "light field", "image quality", "video quality", "sr", "upscaling", "color",
)

# 评测/评审类赛道（画质评价、异常检测、审美评测）离「复原」较远，同届里降权，
# 让超分/去噪/去雾这些栏目主赛道先入选
_OFFTOPIC_TRACK_WORDS = ("quality assessment", "anomaly detection", "aesthetic",
                         "perceptual study", "benchmark on", "efficiency track")
_ORG_RE = re.compile(
    r"Universit|Institute|Laborator|\bLab\b|\bLabs\b|\bInc\b|\bLtd\b|\bCorp|College|"
    r"Academy|Research|Sony|Samsung|Huawei|OPPO|vivo|Xiaomi|ByteDance|Alibaba|"
    r"Tencent|Baidu|Adobe|Meta\b|Google|Microsoft|NVIDIA|SenseTime|CUHK|Tsinghua|"
    r"Peking|Zhejiang|SJTU|Fudan|NJU\b|USTC|ETH\b|NUS\b|NTU\b|KAIST|Team\s",
    re.I,
)
_SECTION_SPLIT_RE = re.compile(r"\n\s*(\d)\.(\d)\.\s*")
# 「3.x Submitted Methods」「3.1 Quantitative Comparison」这类是章节导语，不是某支队伍的方案
_GENERIC_SECTION_RE = re.compile(
    r"^(submitted methods|introduction|conclusion|discussion|results?|"
    r"quantitative comparison|teams?|overview|summary|references)", re.I)
# 参赛规模：participants / registrations 才算人数，submissions 是提交次数，不能混用
_PARTICIPANT_RE = re.compile(
    r"(\d[\d,]{1,6})\s+(?:registered\s+)?(participants|registrations|registrants)", re.I)
_SUBMISSION_RE = re.compile(r"(\d[\d,]{1,6})\s+submissions", re.I)


# --------------------------------------------------------------- 1. 发现
def _workshop_editions(html: str) -> list[tuple[str, int, str]]:
    """从菜单页拆出「近 N 届 workshop」入口，返回 [(会议名, 年份, url)]。

    菜单页的长相是 `<a href="CVPR2026_workshops">Workshops</a>`：
    CVPR2026 这类最新一届的 href 没有 `/menu` 后缀，其余是 `/XXX_workshops/menu`，
    两种都要认，否则最新一届永远抓不到。
    """
    if not (BeautifulSoup and html):
        return []
    soup = BeautifulSoup(html, "lxml")
    out: dict[tuple[str, int], str] = {}
    for a in soup.select("a[href]"):
        href = str(a.get("href") or "")
        text = a.get_text(" ", strip=True)
        if "workshop" not in href.lower() and "workshop" not in text.lower():
            continue
        m = re.search(r"/?([A-Z]{3,5})(20\d{2})_workshops", href)
        if not m:
            continue
        conf, year = m.group(1), int(m.group(2))
        if not href.startswith("http"):
            href = abs_url(CVF_BASE + "/", href.lstrip("/") if href.startswith("/") else href)
        if "/menu" not in href:
            href = href.rstrip("/") + "/menu"
        out.setdefault((conf, year), href)
    return sorted(((c, y, u) for (c, y), u in out.items()), key=lambda t: -t[1])


def _ntire_link(html: str) -> str:
    """workshop 菜单页里找 NTIRE 的入口（该届没有 NTIRE 就返回空，直接跳过这一届）。"""
    if not (BeautifulSoup and html):
        return ""
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a[href]"):
        href = str(a.get("href") or "")
        text = a.get_text(" ", strip=True)
        if "ntire" in href.lower() or "ntire" in text.lower():
            return href if href.startswith("http") else abs_url(CVF_BASE + "/", href.lstrip("/"))
    return ""


def split_report_title(title: str) -> tuple[str, str]:
    """把报告标题拆成 (年份, 赛道名)。

    「NTIRE 2025 Challenge on Short-form UGC Video Quality Assessment: KwaiSR Dataset」
    → ("2025", "Short-form UGC Video Quality Assessment")
    「RAW Image Reconstruction from RGB on Smartphones. NTIRE 2025 Challenge Report」
    → ("2025", "RAW Image Reconstruction from RGB on Smartphones")
    带分赛道（Track 1/2）的报告要把赛道号留在名字里：社区检索是否命中同一赛道全靠它。
    """
    title = strip_html(title, 300)
    year = ""
    m = re.search(r"NTIRE\s*(20\d{2})", title, re.I)
    if m:
        year = m.group(1)
    else:
        m2 = re.search(r"(20\d{2})", title)
        year = m2.group(1) if m2 else ""

    rest = title
    m3 = re.search(r"NTIRE\s*20\d{2}\s*challenge\s*(?:on|for)?\s*(.*)$", title, re.I)
    if m3 and m3.group(1).strip():
        rest = m3.group(1)
    else:
        rest = re.sub(r"\bNTIRE\s*20\d{2}\b", " ", title, flags=re.I)
        rest = re.sub(r"challenge\s*report|challenge", " ", rest, flags=re.I)
    rest = strip_html(rest, 200).strip(" .-–—")

    track = re.split(r"[：:]", rest)[0]
    track = strip_html(track, 160).strip(" .-–—")
    # 去掉「The 3rd」「The Eleventh」「at」这类序数与连接词：
    # 赛道名要能直接当搜索关键词用，带序数会污染检索（「NTIRE 2026 The 3rd ... 冠军」）
    for _ in range(3):
        track = re.sub(r"^(?:the\s+)?(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|"
                       r"sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)\s+", "",
                       track, flags=re.I).strip()
    track = re.sub(r"\s+(?:at|in)\s*$", "", track, flags=re.I).strip(" .-–—")
    # 「The Second Challenge on Event-Based Image Deblurring」去掉序数和 challenge 后
    # 会剩下一个孤零零的「on」，不剥掉的话赛道名和检索词都会带上它
    track = re.sub(r"^(?:on|for|of)\s+", "", track, flags=re.I).strip(" .-–—")
    tr = re.search(r"track\s*(\d+)", rest, re.I)
    if tr and f"track {tr.group(1)}" not in track.lower():
        track = f"{track} Track {tr.group(1)}".strip()
    return year, track or strip_html(title, 160)


def is_challenge_report(title: str) -> bool:
    """只收官方挑战赛报告：标题必须同时出现 NTIRE + 年份 + challenge。"""
    low = (title or "").lower()
    return ("ntire" in low and "challenge" in low and bool(re.search(r"20\d{2}", low)))


def _lowlevel_score(text: str) -> int:
    low = (text or "").lower()
    return sum(1 for w in _LOWLEVEL_WORDS if w in low)


def parse_ntire_page(html: str, *, conference: str = "", year: int = 0,
                     page_url: str = "") -> list[Item]:
    """解析某一届 NTIRE 页面，产出「学习资源」类型的 Item（不进赛事时效过滤）。"""
    if not (BeautifulSoup and html):
        return []
    soup = BeautifulSoup(html, "lxml")
    rows: list[tuple[str, str]] = []
    for dt in soup.select("dt.ptitle"):
        a = dt.select_one("a[href]")
        if not a:
            continue
        rows.append((dt.get_text(" ", strip=True), str(a.get("href") or "")))
    if not rows:  # 老页面没有 dt.ptitle 时的兜底
        for a in soup.select("a[href$='_paper.html']"):
            rows.append((a.get_text(" ", strip=True), str(a.get("href") or "")))

    items: list[Item] = []
    for raw_title, href in rows:
        if not is_challenge_report(raw_title):
            continue
        abstract_url = href if href.startswith("http") else abs_url(CVF_BASE + "/", href.lstrip("/"))
        rep_year, track = split_report_title(raw_title)
        y = int(rep_year or year or 0) or datetime.now().year
        bonus = _lowlevel_score(track)
        # 分数只服务于「同届里先写哪一篇」：底层视觉赛道优先；评测/评审类赛道降权——
        # 它们同样有价值，但「画质评价」「异常检测」不是栏目读者的第一需求
        offtopic = any(w in track.lower() for w in _OFFTOPIC_TRACK_WORDS)
        score = (8.0 + min(bonus, 5) * 1.2
                 + (1.5 if "report" in raw_title.lower() else 0.0)
                 - (2.5 if offtopic else 0.0))
        item = Item(
            module="contest",
            title=strip_html(raw_title, 200),
            url=abstract_url,
            source="CVF NTIRE 报告",
            published_at=datetime(y, 6 if conference.upper().startswith("CVPR") else 10, 1,
                                  tzinfo=timezone.utc),
            summary=f"{conference} NTIRE {y} 官方挑战赛报告：{track}。含赛道定义、数据规格、"
                    f"评测口径与完整榜单，是这类赛题最权威的一份解法汇总。",
            category="学习资源",
            year=str(y),
            score=score,
            extra={
                "origin": "ntire",
                "is_learning_resource": True,
                "conference": conference,
                "workshop_url": page_url,
                "abstract_url": abstract_url,
                # CVF 的 PDF 与摘要页分目录：.../html/X.html ↔ .../papers/X.pdf
                # 直接按 /html/ → /papers/ 重写，不必再请求摘要页
                "pdf_url": (abstract_url[:-5].replace("/html/", "/papers/") + ".pdf")
                           if abstract_url.endswith(".html") else "",
                "report_title": strip_html(raw_title, 200),
                "year": str(y),
                "track": track,
                "entry_url": abstract_url,
                "platform": "CVF Open Access",
                "prize": "—",
                "deadline": "",
            },
        )
        items.append(item)
    items.sort(key=lambda x: x.score, reverse=True)
    return items


async def fetch_reports(client: HttpClient) -> list[Item]:
    """主入口：近 N 届 workshop → NTIRE 页 → 官方挑战赛报告 Item 列表。"""
    cfg = topics.raw.get("competitions", {}).get("ntire", {})
    if not cfg.get("enabled", True):
        return []
    editions = int(cfg.get("editions", 3))
    max_items = int(cfg.get("max_items", 12))
    menu_url = str(cfg.get("menu_url") or MENU_URL)

    try:
        menu_html = await client.text(menu_url, headers={"User-Agent": UA})
    except Exception as exc:
        print(f"[contest] CVF 菜单页抓取失败：{type(exc).__name__}: {exc}", flush=True)
        return []

    picked: list[Item] = []
    for conf, year, wurl in _workshop_editions(menu_html)[: max(editions + 2, 4)]:
        if len({i.extra.get("year") for i in picked}) >= editions and picked:
            break
        try:
            wmenu = await client.text(wurl, headers={"User-Agent": UA})
        except Exception:
            continue
        link = _ntire_link(wmenu)
        if not link:
            continue
        try:
            page = await client.text(link, headers={"User-Agent": UA})
        except Exception:
            continue
        got = parse_ntire_page(page, conference=f"{conf} {year} Workshops",
                               year=year, page_url=link)
        if got:
            print(f"[contest] CVF NTIRE {year}（{conf}）：{len(got)} 篇挑战赛报告", flush=True)
            picked.extend(got)
        if len(picked) >= max_items:
            break

    # 跨届去重（同一赛道隔年会重名），按分数取前 max_items
    seen: set[str] = set()
    unique: list[Item] = []
    for item in sorted(picked, key=lambda x: x.score, reverse=True):
        key = f"{item.extra.get('year')}|{item.extra.get('track', '')[:40]}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:max_items]


# --------------------------------------------------------------- 2. 深挖
async def fetch_abstract(client: HttpClient, url: str) -> tuple[str, str]:
    """取摘要页的 abstract 与 PDF 链接。PDF 缺失时按 CVF 约定把 .html 换成 .pdf。"""
    if not url:
        return "", ""
    try:
        html = await client.text(url, headers={"User-Agent": UA})
    except Exception:
        return "", ""
    soup = BeautifulSoup(html, "lxml") if BeautifulSoup else None
    abstract, pdf = "", ""
    if soup:
        node = soup.select_one("#abstract")
        if node:
            abstract = strip_html(node.get_text(" ", strip=True), 2600)
        for a in soup.select("a[href$='.pdf']"):
            href = str(a.get("href") or "")
            if href:
                pdf = href if href.startswith("http") else abs_url(CVF_BASE + "/", href.lstrip("/"))
                break
    if not pdf and url.endswith(".html"):
        pdf = url[:-5] + ".pdf"
    return abstract, pdf


async def _pdf_bytes(client: HttpClient, url: str, max_bytes: int) -> bytes:
    """分段下载报告前 max_bytes 字节；正文抽取与封面抠图共用这一次下载。"""
    if not url:
        return b""
    headers = {"User-Agent": UA, "Range": f"bytes=0-{max_bytes}"}
    try:
        data, _ = await client.get_bytes(url, headers=headers)
    except Exception:
        return b""
    return data


def _text_from(data: bytes) -> str:
    """PDF 字节抽正文；PyMuPDF 缺失或文件不完整时返回空串。"""
    try:
        try:
            import pymupdf as fitz  # PyMuPDF ≥ 2.0 的规范名
        except ImportError:  # pragma: no cover
            import fitz  # type: ignore
    except Exception:
        return ""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        return "".join(page.get_text() for page in doc)
    except Exception:
        return ""


async def _pdf_text(client: HttpClient, url: str, max_bytes: int) -> str:
    """分段下载报告前 max_bytes 字节并抽正文。"""
    return _text_from(await _pdf_bytes(client, url, max_bytes))


def parse_sections(text: str, limit: int = 8) -> list[dict]:
    """从报告正文里拆「3.x 方法名 + 机构/队伍」小节。

    NTIRE 报告的固定写法是「3.1. <方法名>\\n<机构>\\n<作者>」，且只描述 top 提交，
    所以第一个小节通常就是冠军方案。**必须先定位到方法章节再切**：报告前半部分是
    数据集小节（2.1 训练集 / 2.2 验证集 / 2.3 指标），直接按 3.x 切会把数据章节
    当成参赛方案，写进正文就是硬伤。
    """
    if not text:
        return []
    anchor = -1
    for pat in (r"\n\s*\d\.\s*(?:challenge\s+|proposed\s+)?methods",
                r"\n\s*\d\.\s*teams?\s*(?:&|and)\s*affiliations",
                r"\n\s*\d\.\s*challenge\s+entries"):
        m = re.search(pat, text, re.I)
        if m:
            anchor = m.start()
            break
    if anchor < 0:
        return []
    body = text[anchor:]
    parts = _SECTION_SPLIT_RE.split(body)
    out: list[dict] = []
    for i in range(1, len(parts) - 2, 3):
        chunk = parts[i + 2]
        lines = [ln.strip() for ln in chunk.split("\n") if ln.strip()]
        if not lines:
            continue
        head = " ".join(lines[:2])[:160]
        head = re.sub(r"-\s+(?=[a-z])", "", head)  # 断词连字符：「Re- construction」→「Reconstruction」
        if _GENERIC_SECTION_RE.match(head):
            continue  # 导语型小节跳过，避免把「Submitted Methods」写成一支队伍
        team = ""
        for line in lines[1:5]:
            if _ORG_RE.search(line) and len(line) <= 90:
                team = line
                break
        if not team:
            m = re.search(r"Team\s+([A-Za-z0-9][\w\-\.& ]{2,40})", chunk[:800])
            team = m.group(1).strip() if m else ""
        out.append({"num": f"{parts[i]}.{parts[i + 1]}", "method": head, "team": team})
        if len(out) >= limit:
            break
    return out


_ROW_RE = re.compile(r"(?:^|\s)(\d{1,2})\s+([A-Za-z][A-Za-z0-9\-\.& ]{1,38}?)\s+(\d\.\d{3,6})")


def parse_leaderboard(text: str, limit: int = 5) -> list[dict]:
    """从榜单表里抽名次 / 队伍 / 分数。

    报告里的 Table 1 通常是「Award Ranking」，正文抽出来的形态是
    「1 IH-VQA 0.7305 0.6943 0.7667 2 VCIP Pi Group 0.7253 …」，按行拆不靠谱，
    这里直接做「名次 + 队名 + 首个分数」的三元组扫描，并要求名次连续递增。
    """
    if not text:
        return []
    start = -1
    for pat in (r"Table\s*\d+\.\s*Award\s+Ranking", r"Table\s*\d+\.\s*Leaderboard",
                r"Award\s+Ranking", r"Leaderboard of the"):
        m = re.search(pat, text, re.I)
        if m:
            start = m.start()
            break
    if start < 0:
        return []
    seg = text[start: start + 1400]
    rows: list[dict] = []
    expect = 1
    for m in _ROW_RE.finditer(seg):
        rank = int(m.group(1))
        team = strip_html(m.group(2), 48).strip()
        if rank != expect or not team or len(team) < 2:
            continue
        rows.append({"rank": rank, "team": team, "score": m.group(3)})
        expect += 1
        if rank >= limit:
            break
    return rows


def report_stats(text: str) -> dict:
    """参赛规模与指标：报告里的「192 registered participants」这类硬信息。"""
    stats: dict = {}
    if not text:
        return stats
    m = _PARTICIPANT_RE.search(text)
    if m:
        stats["participants"] = m.group(1)
        # 连原句一起留下：模型引用整句比引用裸数字安全，数字脱离上下文极易写错口径
        s = max(0, m.start() - 50)
        stats["participants_hint"] = strip_html(text[s: m.end() + 60], 180)
    m2 = _SUBMISSION_RE.search(text)
    if m2:
        stats["submissions"] = m2.group(1)
    m3 = re.search(r"(?:two|three|four|five|\d+)\s+tracks", text, re.I)
    if m3:
        stats["tracks"] = m3.group(0).strip()
    metrics = [k for k in ("PSNR", "SSIM", "LPIPS", "DISTS", "MOS", "VMAF", "FID", "MUSIQ")
               if k in text]
    if metrics:
        stats["metrics"] = metrics[:5]
    return stats


def benchmark_hints(text: str, limit: int = 3) -> list[str]:
    """榜单表说明（含指标与口径），给模型做「评测怎么打分」的事实依据。"""
    if not text:
        return []
    out: list[str] = []
    for m in re.finditer(r"Table\s*\d+\.", text):
        seg = strip_html(text[m.start(): m.start() + 400], 400)
        if len(seg) > 40:
            out.append(seg)
        if len(out) >= limit:
            break
    return out


def results_table(text: str, limit: int = 1600) -> str:
    """结果表的原始文本（表头 + 前几行数字）。

    这是「榜单与差距」那一节唯一可用的事实底座：报告里的表是「团队名 + 一串数字」的
    多列排版，抽成结构化行会失真，但整段原样交给模型，它能自己读出 PSNR 与运行时。
    没有这一段，模型只能写「报告没有给出完整榜单」——那种句子是硬伤。
    """
    if not text:
        return ""
    for m in re.finditer(r"Table\s*1\.[^\n]{0,200}", text):
        seg = text[m.start(): m.start() + limit]
        if len(re.findall(r"\d+(?:\.\d+)?", seg)) >= 8:
            return strip_html(seg, limit)
    return ""


# --------------------------------------------------- 3. 社区：冠军方案定向检索
def _track_tokens(track: str) -> list[str]:
    return [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", track or "")][:6]


# 英文赛道名 vs 中文社区标题：只靠 ASCII 词做匹配会漏掉「2026 图像去噪实战」这类标题，
# 所以补一层中文领域词（社区文章几乎不会写英文 track 名）
_CN_TRACK_WORDS = ("超分", "去噪", "降噪", "去模糊", "去雨", "去雾", "低光", "低照",
                   "增强", "复原", "修复", "插帧", "压缩", "去马赛克", "深度估计", "HDR",
                   "RAW", "画质", "质量评价", "图像质量", "夜景")


def is_relevant_hit(text: str, year: str, track: str) -> bool:
    """社区结果相关性：必须提到 NTIRE；退一步要求「年份 + 赛道词」同时出现。

    CSDN 的搜索常年混进「低照度图像处理算法综述」这类泛文，标题里没有 NTIRE 就不能要——
    素材会被写进「冠军选手怎么调」那一节，跑题等于编造。
    """
    low = (text or "").lower()
    if "ntire" in low:
        return True
    if not (year and year in low):
        return False
    if any(tok in low for tok in _track_tokens(track)):
        return True
    return any(w.lower() in low for w in _CN_TRACK_WORDS)


def _track_no(track: str) -> str:
    m = re.search(r"track\s*(\d+)", track or "", re.I)
    return m.group(1) if m else ""


def track_conflict(text: str, track: str) -> bool:
    """命中结果讲的是同届的**另一条赛道**（Track 2 vs Track 1）。

    这种情况不能直接扔——同届赛道的复盘能佐证整届的技术趋势；但要打上 same_track=False，
    否则模型会把「Track 2 的冠军方案」写成「这条赛道的冠军方案」，是硬伤。
    """
    want = _track_no(track)
    if not want:
        return False
    found = re.search(r"track\s*(\d+)", text or "", re.I)
    return bool(found) and found.group(1) != want


def _clean_csdn_title(title: str) -> str:
    return strip_html(re.sub(r"</?em>", "", title or ""), 140)


async def search_csdn(client: HttpClient, query: str, year: str, track: str) -> list[dict]:
    """CSDN 官方搜索接口（免 key，返回 JSON，摘要质量高）。"""
    try:
        data = await client.json("https://so.csdn.net/api/v3/search",
                                 params={"q": query, "t": "all", "p": 1},
                                 headers={"User-Agent": UA})
    except Exception:
        return []
    rows = data.get("result_vos") if isinstance(data, dict) else None
    out: list[dict] = []
    for row in (rows or [])[:20]:
        title = _clean_csdn_title(str(row.get("title") or ""))
        snippet = strip_html(str(row.get("digest") or row.get("description") or ""), 320)
        url = str(row.get("url") or "").split("?")[0]
        if not (title and url):
            continue
        if not is_relevant_hit(f"{title} {snippet}", year, track):
            continue
        out.append({"title": title, "snippet": snippet, "url": url, "source": "CSDN"})
    return out


async def search_zhihu(client: HttpClient, query: str, year: str, track: str) -> list[dict]:
    """知乎：站内搜索接口要签名，改走 Bing 的 site: 检索（免 key，稳定）。"""
    from .competitions import _bing_results  # 复用已有的 Bing 页面解析

    try:
        rows = await _bing_results(client, f"site:zhihu.com {query}")
    except Exception:
        return []
    out: list[dict] = []
    for row in rows:
        title = strip_html(str(row.get("title") or ""), 140)
        snippet = strip_html(str(row.get("snippet") or ""), 320)
        url = str(row.get("link") or "")
        if not (title and url) or "zhihu.com" not in url:
            continue
        if not is_relevant_hit(f"{title} {snippet}", year, track):
            continue
        out.append({"title": title, "snippet": snippet, "url": url, "source": "知乎"})
    return out


def community_queries(item: Item, champion: dict) -> list[str]:
    """「NTIRE + 年份 + 赛道 + 冠军/解读」三组定向查询。"""
    year = str(item.extra.get("year") or "")
    track = item.extra.get("track") or ""
    short = re.split(r"[：:]", track)[0][:52]
    qs: list[str] = []
    method = str((champion or {}).get("method") or "")
    name = re.split(r"\s*[-–—]\s*", method)[0][:36].strip(" .")
    if name:
        qs.append(f"NTIRE {year} {name} 冠军 方案")
    qs.append(f"NTIRE {year} {short} 冠军 方案")
    qs.append(f"NTIRE {year} {short} 解读 复盘")
    return qs


def rank_hits(rows: list[dict], champion: dict, track: str = "", limit: int = 5) -> list[dict]:
    """按「是否点名冠军 / 是否讲方案 / 是否同赛道」排序并去重。

    same_track=False 的条目保留但降权：它们是同届其他赛道的复盘，只能佐证趋势。
    """
    method = str((champion or {}).get("method") or "")
    team = str((champion or {}).get("team") or "")
    name = re.split(r"\s*[-–—]\s*", method)[0].lower().strip()
    scored: list[tuple[float, dict]] = []
    seen: set[str] = set()
    for row in rows:
        url = row.get("url") or ""
        if not url or url in seen:
            continue
        seen.add(url)
        text = f"{row.get('title', '')} {row.get('snippet', '')}"
        low = text.lower()
        s = 0.0
        if name and name[:12] in low:
            s += 3.0
        if team and team.lower()[:10] in low:
            s += 2.5
        if any(w in text for w in ("冠军", "第一名", "夺冠", "winner")):
            s += 2.0
        if any(w in text for w in ("方案", "复盘", "解读", "解析", "实战", "经验")):
            s += 1.0
        conflict = track_conflict(text, track)
        if conflict:
            s -= 1.5
        s += min(len(row.get("snippet") or ""), 300) / 600
        scored.append((s, {**row, "same_track": not conflict}))
    scored.sort(key=lambda t: -t[0])
    return [row for _, row in scored[:limit]]


async def search_community(client: HttpClient, item: Item, champion: dict,
                           limit: int = 5) -> list[dict]:
    """冠军方案定向检索：CSDN 接口 + 知乎（Bing site:）并发跑，合并后统一排序。

    两轮：先按「带赛道号」的精确查询；同赛道命中不足时再退一轮不带赛道号的宽查询，
    把同届其他赛道的复盘也捞进来（打 same_track=False），总比整节没素材好。
    """
    import asyncio

    year = str(item.extra.get("year") or "")
    track = item.extra.get("track") or ""
    qs = community_queries(item, champion)

    async def _run(queries: list[str]) -> list[dict]:
        tasks = []
        for q in queries:
            tasks.append(asyncio.create_task(search_csdn(client, q, year, track)))
        tasks.append(asyncio.create_task(search_zhihu(client, queries[0], year, track)))
        hits: list[dict] = []
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, list):
                hits += res
        return hits

    hits = await _run(qs[:2])
    ranked = rank_hits(hits, champion, track, limit)
    if len(ranked) < 2:
        broad = [f"NTIRE {year} {re.split(r'[：:]', track)[0][:40]} 冠军 方案"]
        hits += await _run(broad)
        ranked = rank_hits(hits, champion, track, limit)
    return ranked


def _pick_champion(sections: list[dict], board: list[dict]) -> dict:
    """冠军方案：榜单第一名 > 方法章节第一小节。

    两者都有时把「队名 + 方法名 + 分数」拼到一起——单靠方法名搜不到人，
    单靠队名又讲不清方案，社区检索和正文都需要这一对。
    """
    top = board[0] if board else {}
    method = ""
    team = str(top.get("team") or "")
    if team:
        low = team.lower()
        for s in sections:
            st = str(s.get("team") or "").lower()
            mt = str(s.get("method") or "").lower()
            # 队名可能出现在 team 行（Sony AI），也可能直接写进方法标题（DualRAW - ...），
            # 两处都要认，否则拿不到「队伍 ↔ 方法」的对应关系
            if (st and (low in st or st in low)) or (mt and low in mt):
                method = str(s.get("method") or "")
                break
    if not team and sections:
        team = str(sections[0].get("team") or "")
        method = str(sections[0].get("method") or "")
    if not (team or method):
        return {}
    champ = {"rank": 1, "team": team, "method": method}
    if top.get("score"):
        champ["score"] = top["score"]
    return champ


async def enrich_report(client: HttpClient, item: Item) -> dict:
    """一份 NTIRE 报告的完整素材：官方正文 + 冠军方案 + 榜单要点 + 社区复盘。"""
    import asyncio

    cfg = topics.raw.get("competitions", {}).get("ntire", {})
    max_bytes = int(cfg.get("pdf_bytes", 800_000))
    url = str(item.extra.get("abstract_url") or item.url)
    abstract, pdf = await fetch_abstract(client, url)
    text, cover_cands = "", []
    if pdf:
        try:
            data = await asyncio.wait_for(_pdf_bytes(client, pdf, max_bytes), timeout=75.0)
        except Exception:
            data = b""
        if data:
            from . import pdf_figs

            try:
                text = _text_from(data)
            except Exception:
                text = ""
            # 顺手把报告里的数据集样例 / teaser 抠成封面候选：封面阶段不必再拉一次 PDF
            try:
                for fig in pdf_figs.extract(data, max_pages=8, limit=2):
                    png = pdf_figs.to_png(fig)
                    if not png:
                        continue
                    import hashlib
                    digest = hashlib.md5(
                        f"{item.fingerprint}:{fig.page}".encode()).hexdigest()[:16]
                    path = settings.image_dir / f"cover_{digest}.png"
                    path.write_bytes(png)
                    cover_cands.append({"path": str(path), "caption": fig.caption,
                                        "role": fig.kind, "source": "report-pdf",
                                        "referer": pdf})
            except Exception:
                cover_cands = []
    sections = parse_sections(text)
    board = parse_leaderboard(text)
    champion = _pick_champion(sections, board)
    detail = {
        "source": "CVF Open Access · NTIRE 官方挑战赛报告",
        "abstract": abstract,
        "pdf_url": pdf,
        "report_excerpt": strip_html(text, 9000),
        "sections": sections[:6],
        "teams": [s["team"] for s in sections if s.get("team")][:8],
        "leaderboard": board[:5],
        "champion": champion,
        "stats": report_stats(text or abstract),
        "benchmark": benchmark_hints(text),
        "results_table": results_table(text),
        "cover_candidates": cover_cands,
        "community": [],
        "fetched": bool(abstract or text),
    }
    try:
        detail["community"] = await asyncio.wait_for(
            search_community(client, item, champion), timeout=45.0)
    except Exception:
        detail["community"] = []
    return detail
