"""封面图选择：三个模块要的不是同一种图。

paper    —— 核心架构图（首选）> 亮眼定性效果图（次选）> teaser / SOTA 对比图（第三）。
            封面要回答「这篇论文解决了什么」，所以论文自己的 pipeline 图永远优先于任何新闻配图。
industry —— 系统架构图 / 官方 logo / 代表图。封面要回答「谁干了什么」，
            仓库 README 的头图、项目官网 og:image 都可以，logo 允许走 contain 版式。
contest  —— 官方视觉 / 数据集样例 / baseline 框架图。**没有就退品牌底图**，
            赛事稿凑一张不相干的图，比纯底图更廉价。
            四级渐进（用户铁律，2026-11-01 定稿）：
            ① 官方直采（og:image / 宣传 Banner / 主视觉 / 官方 Logo）
            ② 相关匹配（数据集样例、baseline 框架示意、知乎/CSDN 冠军复盘的方案图）
            ③ 尽力而为：全无时退品牌底图，**不报错、不卡死**
            ④ 不强求：绝不为凑封面抓一张与赛题无关的图

三条硬规则（也是这类系统最容易翻车的地方）：
1. 只从「这条素材自己的页面」取图（论文 PDF/HTML、仓库 README、赛事页、媒体正文），
   不跨素材借图——封面与正文对不上，比没封面更伤；
2. 每张候选都要过体检：尺寸、比例、纯色/空白、badge/头像/二维码一律丢弃。
   logo 走 contain（居中留白），否则 2.35:1 裁切会把 wordmark 切成半个字母；
3. 分数不到阈值就返回 None，由渲染层走品牌底图——「宁缺毋滥」写进代码，
   而不是指望每次人工判断。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import settings, topics
from ..models import Item

# ---------------------------------------------------------------- 角色判定
# 匹配顺序 = 论文封面优先级：arch（首选）→ result（次选）→ teaser（第三）。
# 「overview of our pipeline vs. SOTA」「Figure 1: visual comparison」这种图注
# 两边都命中：前者是架构图归 arch，后者是效果图归 result——效果对比图
# 比「总览图」更配当封面，所以 result 关键词先于 teaser 判定。
_ROLE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("arch", ("architecture", "architectur", "overview", "pipeline", "framework",
              "network structure", "network", "diagram", "flowchart", "overall",
              "架构", "框架", "流程图", "整体结构", "网络结构")),
    # product：核心软硬件产品图——芯片/板卡/SoC/传感器/实车/机器人/样机渲染。
    # 放在 result 之前：产业稿里「芯片特写」「板卡实拍」的判定权高于泛化的效果词
    ("product", ("chip", "soc", "board", "sensor", "module", "prototype", "device",
                 "hardware", "product", "unboxing", "hands-on", "render",
                 "芯片", "板卡", "模组", "传感器", "样机", "原型机", "实拍", "实车",
                 "车端", "整机", "产品图", "渲染图", "开箱")),
    # venue：发布会 / 展台现场（XHEART 舞台大屏这类主视觉）
    ("venue", ("keynote", "unveiled", "onstage", "launch event", "exhibition",
               "发布会", "发布现场", "现场", "展台", "开幕", "大会")),
    ("result", ("qualitative", "comparison", "compared", "visual result", "results",
                "psnr", "ssim", "visualization", "对比", "效果图", "定性")),
    ("teaser", ("teaser", "figure 1", "fig. 1", "main idea", "highlight", "亮点", "总览")),
    ("dataset", ("dataset", "data sample", "samples of", "example images", "样例", "数据集")),
    ("screenshot", ("screenshot", "mainwindow", "main window", "window", "interface",
                    "web ui", "dashboard", "settings", "截图", "界面")),
    ("logo", ("logo", "wordmark", "emblem", "brand mark", "标识", "图标")),
)
# URL 里出现即丢弃：badge、头像、统计计数、占位图、二维码
_BAD_URL = ("badge", "shields.io", "badgen", "visitor", "counter", "hitcount",
            "gravatar", "avatar", "qrcode", "qr-code", "1x1", "pixel.gif",
            "spacer", "blank.gif", "loading", "placeholder", "emoji", "flag-icon",
            "/icon", "icon_", "favicon")
# 论文封面绝不能出现 logo / 徽标——学术稿的封面放一个公司 logo，直接出戏
_ACCEPT_ROLES = {
    "paper": {"arch", "teaser", "result", "dataset", "screenshot", "other"},
    # industry 放开 other：媒体正文里的头图 / 配图本身就是「产业动态的视觉」，
    # 不能因为图注没写「架构」二字就整个弃掉。
    # product/venue/body-hero：核心产品图、发布会现场、报道主视觉（正文首图）
    "industry": {"arch", "product", "venue", "body-hero", "logo", "screenshot",
                 "teaser", "result", "dataset", "other"},
    "contest": {"official", "logo", "dataset", "arch", "venue", "related",
                "teaser", "result"},
}
# 模块内角色权重：数值本身不重要，重要的是「同一份素材里谁排前面」。
# paper 按用户铁律排序：核心架构图（首选）> 亮眼定性效果图（次选）>
# teaser / SOTA 对比图（第三）——「Figure 1 = teaser」的老权重已推翻。
# industry 按用户铁律排序：核心产品图/芯片架构（首选）> 报道主视觉（正文首图，
# 多为发布会现场/产品大图）> logo > 截图——「other 仅 1.2 分」的老权重是
# 「封面空洞」的直接原因：量子位正文图没有图注，全被判 other 后必被阈值拦下。
_WEIGHTS = {
    "paper": {"arch": 5.2, "result": 4.6, "teaser": 4.0, "dataset": 1.2,
              "screenshot": 0.6, "other": 2.4, "logo": -5.0},
    "industry": {"arch": 4.6, "product": 4.4, "body-hero": 4.2, "venue": 4.0,
                 "logo": 3.4, "screenshot": 3.2, "teaser": 2.8, "result": 2.6,
                 "dataset": 1.8, "other": 1.2},
    # contest 按用户四级铁律排序：官方直采（主视觉/Banner > 官方 Logo）>
    # 相关匹配（数据集样例 > baseline 框架图 > 赛事现场）> 兜底 related（官方页首图）。
    # teaser / result 留在白名单里是历史兼容：部分平台把横幅塞在 teaser 语义下。
    "contest": {"official": 5.0, "logo": 4.4, "dataset": 4.2, "arch": 4.0,
                "venue": 3.8, "related": 2.8, "teaser": 2.6, "result": 2.4,
                "screenshot": 1.2, "other": 0.4},
}
# 来源加成：官方页面 > 论文/仓库原始素材 > 媒体转述 > 自动生成卡片
_SOURCE_BONUS = {"official": 0.8, "report-pdf": 0.7, "paper-pdf": 0.6,
                 "arxiv-html": 0.6, "readme": 0.5, "homepage": 0.4,
                 "media": 0.2, "og": -0.2}
_DEFAULT_MIN = {"paper": 3.4, "industry": 2.6, "contest": 3.2}

# build_figures / arxiv 兜底写的图注不是真图注，别拿它当「这是正式插图」的证据
_PLACEHOLDER_CAPTIONS = ("论文配图", "实验结果对比图", "论文插图", "自动抓取")
_OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)',
    re.I)


@dataclass
class Candidate:
    url: str = ""
    path: str = ""              # 已在本地（figures 表里的图）就不用再下载
    caption: str = ""
    source: str = "other"       # official | report-pdf | paper-pdf | arxiv-html | readme | media | og
    role: str = "other"
    referer: str = ""
    score: float = 0.0
    width: int = 0
    height: int = 0
    fit: str = "cover"          # cover（裁切铺满）| contain（居中留白，给 logo）
    # 备用地址：raw.githubusercontent 在部分网络下 TLS 直接被掐，换 CDN 镜像再试
    fallbacks: list[str] = field(default_factory=list)
    # 候选来自第几条素材：0 = 本期主角。封面只能出自主角，否则会出现
    # 「文章写 A、封面是 B」——一期多条的模块里这是硬伤，不是风格问题
    order: int = 0


@dataclass
class CoverPick:
    path: str
    url: str = ""
    role: str = "other"
    source: str = ""
    caption: str = ""
    score: float = 0.0
    fit: str = "cover"
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        return (f"{self.role}·{self.source}（{self.score:.1f} 分）"
                + (f"：{self.caption[:28]}" if self.caption else ""))


# ---------------------------------------------------------------- 工具
def classify(caption: str = "", url: str = "", alt: str = "") -> str:
    """按图注 → alt → URL 的顺序判角色；判不出就是 other。"""
    for blob in (caption, alt):
        low = (blob or "").lower()
        if not low:
            continue
        for role, words in _ROLE_WORDS:
            if any(w in low for w in words):
                return role
    name = (url or "").lower().rsplit("/", 1)[-1]
    for role, words in _ROLE_WORDS:
        if any(w.replace(" ", "") in name for w in words if len(w) >= 4):
            return role
    return "other"


def clean_caption(caption: str) -> str:
    cap = re.sub(r"\s+", " ", str(caption or "")).strip()
    if not cap or any(cap.startswith(p) for p in _PLACEHOLDER_CAPTIONS):
        return ""
    return cap


def is_junk_url(url: str) -> bool:
    low = (url or "").lower()
    if not low:
        return True
    if low.startswith("data:"):
        return True
    return any(bad in low for bad in _BAD_URL)


def _cfg() -> dict:
    return topics.raw.get("cover") or {}


def min_score(module: str) -> float:
    cfg = (_cfg().get("min_score") or {}).get(module)
    return float(cfg if cfg is not None else _DEFAULT_MIN.get(module, 3.2))


def _aspect_score(c: Candidate) -> float:
    if not c.width or not c.height:
        return 0.0
    a = c.width / c.height
    if 1.15 <= a <= 2.7:      # 与公众号 2.35:1 接近，裁切损失最小
        return 0.8
    if 0.85 <= a < 1.15:
        return 0.1
    if a < 0.75:              # 竖图裁成横幅会切掉主体
        return -1.2
    if a > 3.2:               # 长条图（图例、色带、徽标条）
        return -0.6
    return 0.0


def score(c: Candidate, module: str) -> float:
    """先按「角色 + 来源 + 图注」算基础分，再按实测尺寸修正。"""
    table = _WEIGHTS.get(module, _WEIGHTS["paper"])
    s = table.get(c.role, 0.5)
    s += _SOURCE_BONUS.get(c.source, 0.0)
    if c.caption:
        s += 0.5                     # 有图注 = 正式插图，不是装饰图
    if c.role == "logo":
        # logo 走 contain 版式，长宽比与放大损失都不该罚它
        return s
    s += _aspect_score(c)
    if c.width >= 900:
        s += 0.5
    elif c.width and c.width < 620:
        s -= 1.0
    if c.height and c.height < 260:
        s -= 0.8
    return s


def _probe(path: str, role: str) -> tuple[bool, int, int, str]:
    """体检：尺寸、比例、是否空白图。返回 (是否可用, 宽, 高, 原因)。"""
    from PIL import Image, ImageStat

    if not path or not Path(path).exists():
        return False, 0, 0, "文件不存在"
    try:
        with Image.open(path) as img:
            w, h = img.size
            if w * h < 60_000:
                return False, w, h, "像素太少"
            if role == "logo":
                if w < 200 or h < 60:
                    return False, w, h, "logo 太小"
            else:
                if w < 380 or h < 150:
                    return False, w, h, "尺寸不足"
            ratio = w / max(h, 1)
            if ratio > 6 or ratio < 0.2:
                return False, w, h, "比例极端"
            # 纯色/空白图：缩到 48×48 看灰度标准差，低于 5 视为无内容
            gray = img.convert("L").resize((48, 48))
            if ImageStat.Stat(gray).stddev[0] < 5:
                return False, w, h, "近似空白"
            return True, w, h, ""
    except Exception as exc:                      # 坏图、截断的下载都算不可用
        return False, 0, 0, f"打不开：{type(exc).__name__}"


# ---------------------------------------------------------------- 候选收集
def _from_items(items: list[Item], module: str) -> list[Candidate]:
    out: list[Candidate] = []
    for rank, item in enumerate(items):
        # 第一条素材是本期主角，同分时应该优先：给个微小加权
        bonus = 0.3 if rank == 0 else 0.0
        # cover_candidates（正文抽取产物，带图注与 body-hero 角色）必须先注册：
        # 同一张图在列表页 images 里只有缩略地址、没有图注，若让它先占坑，
        # rank() 的 seen 去重会把更精的信息挡在门外（实测 Momenta 稿因此
        # 全部掉进 1.2 分的 other，只能靠救场上封面）
        cand_urls: set[str] = set()
        for cand in item.extra.get("cover_candidates") or []:
            if not isinstance(cand, dict) or not cand.get("url"):
                continue
            url = str(cand["url"])
            cand_urls.add(url.split("?")[0])
            caption = clean_caption(str(cand.get("caption") or ""))
            role = str(cand.get("role") or "")
            if not role:
                role = classify(caption, url)
                # 报道正文首图 = 编辑选的主视觉（多为产品图/发布会现场），
                # 词表判不出角色时按 body-hero 计分，不再掉进 1.2 分的 other。
                # 仅 industry 认这个角色：paper 的白名单里没有 body-hero，
                # 若在这里无差别赋值，业界候选流到论文模块时会被当成新角色漏过白名单
                if (int(cand.get("order") or 0) == 0 and role == "other"
                        and module == "industry"):
                    role = "body-hero"
            out.append(Candidate(
                url=url, path=str(cand.get("path") or ""), caption=caption,
                source=str(cand.get("source") or "other"),
                role=role,
                referer=str(cand.get("referer") or item.url), score=bonus, order=rank))
        caps = item.extra.get("figure_captions") or []
        for idx, src in enumerate(item.images or []):
            if not src:
                continue
            if str(src).split("?")[0] in cand_urls:
                continue
            raw = caps[idx] if idx < len(caps) else (
                item.extra.get("figure_caption") if idx == 0 else "")
            caption = clean_caption(str(raw or ""))
            out.append(Candidate(url=src, caption=caption,
                                 source="arxiv-html" if module == "paper" else "media",
                                 role=classify(caption, str(src)),
                                 referer=item.url, score=bonus, order=rank))
        if item.image_url and item.image_url not in (item.images or []):
            if str(item.image_url).split("?")[0] not in cand_urls:
                out.append(Candidate(url=item.image_url, caption="",
                                     source="media", role=classify("", item.image_url),
                                     referer=item.url, score=bonus, order=rank))
    return out


def _from_figures(figures: dict[str, dict], module: str,
                  items: list[Item] | None = None) -> list[Candidate]:
    """build_figures 已经下好的图：不再走网络，直接用本地路径。"""
    owner = {f"{i.module}:{i.fingerprint}": n for n, i in enumerate(items or [])}
    out: list[Candidate] = []
    for ref, fig in (figures or {}).items():
        path = str(fig.get("path") or "")
        if not path:
            continue
        rel = path if Path(path).exists() else str(settings.out_dir / path)
        caption = clean_caption(str(fig.get("caption") or ""))
        out.append(Candidate(
            url=str(fig.get("url") or ""), path=rel, caption=caption,
            source="arxiv-html" if module == "paper" else "media",
            role=classify(caption, str(fig.get("url") or "")),
            referer="", order=owner.get(str(fig.get("item_id") or ""), 0)))
    return out


def _from_visuals(visuals: list[dict], module: str, referer: str = "") -> list[Candidate]:
    """页面视觉抽取产物（``contest_visual.extract_visuals``）→ 封面候选。

    role 已经判好的（official / logo）直接沿用，不重新按图注猜；
    没判的交给词表；词表也判不出时，只有**正文第一张**升格 related
    （官方页首图通常是主视觉或任务样例），其余原样留给 rank() 的白名单淘汰——
    赛事稿宁可没图，也不放一张说不出是什么的图。
    """
    out: list[Candidate] = []
    for vis in visuals or []:
        url = str(vis.get("url") or "")
        if not url:
            continue
        caption = clean_caption(str(vis.get("caption") or ""))
        role = str(vis.get("role") or "")
        if not role:
            role = classify(caption, url)
            if role == "other" and int(vis.get("body_no") or 0) == 0:
                role = "related"
        out.append(Candidate(url=url, caption=caption,
                             source=str(vis.get("source") or "official"),
                             role=role,
                             referer=str(vis.get("referer") or referer)))
    return out


async def _pdf_candidates(client, item: Item, module: str, source: str,
                          url: str) -> list[Candidate]:
    from ..sources import pdf_figs

    if not url:
        return []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; LLCV-Bot/1.0)",
               "Range": "bytes=0-900000"}
    try:
        # openaccess.thecvf.com 在慢网络下 0.9MB 要 50–80s（实测），45s 会白等一场
        data, _ = await asyncio.wait_for(client.get_bytes(url, headers=headers), timeout=95)
    except Exception:
        return []
    figs = pdf_figs.extract(data, max_pages=8, limit=3)
    out: list[Candidate] = []
    for fig in figs:
        png = pdf_figs.to_png(fig)
        if not png:
            continue
        digest = __import__("hashlib").md5(
            f"{item.fingerprint}:{fig.page}:{fig.caption[:40]}".encode()).hexdigest()[:16]
        path = settings.image_dir / f"cover_{digest}.png"
        path.write_bytes(png)
        role = {"dataset": "dataset", "teaser": "teaser", "arch": "arch",
                "result": "result"}.get(fig.kind, "other")
        out.append(Candidate(url=url, path=str(path), caption=fig.caption,
                             source=source, role=role))
    return out


async def _readme_candidates(client, repo: str) -> list[Candidate]:
    """GitHub README 头图：官方仓库自己放的架构图 / logo / 演示截图。"""
    from bs4 import BeautifulSoup

    try:
        html = await asyncio.wait_for(
            client.text(f"https://api.github.com/repos/{repo}/readme",
                        headers={"Accept": "application/vnd.github.html+json"}), timeout=20)
    except Exception:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []
    raw_base = f"https://raw.githubusercontent.com/{repo}/HEAD"
    cdn_base = f"https://gcore.jsdelivr.net/gh/{repo}@HEAD"
    out: list[Candidate] = []
    for img in soup.find_all("img"):
        src = str(img.get("src") or "")
        if not src or is_junk_url(src):
            continue
        # README 里的相对路径（/Screenshot/a.jpg）在 github.com 域下是 404，
        # 必须换成 raw 形式才能取到原图；raw.githubusercontent 在部分网络下
        # TLS 直接握手失败（实测本机就如此），所以再挂一条 jsDelivr 镜像
        fallbacks: list[str] = []
        if src.startswith("/"):
            fallbacks = [cdn_base + src, f"https://cdn.jsdelivr.net/gh/{repo}@HEAD{src}"]
            src = raw_base + src
        elif not src.startswith(("http://", "https://", "data:")):
            rel = src.lstrip("./")
            fallbacks = [f"{cdn_base}/{rel}", f"https://cdn.jsdelivr.net/gh/{repo}@HEAD/{rel}"]
            src = f"{raw_base}/{rel}"
        alt = str(img.get("alt") or "")
        caption = alt or str(img.get("title") or "")
        out.append(Candidate(url=src, caption=caption, source="readme",
                             role=classify(caption, src, alt),
                             referer=f"https://github.com/{repo}",
                             fallbacks=fallbacks))
        if len(out) >= 8:
            break
    return out


async def _homepage_og(client, url: str, referer: str = "") -> list[Candidate]:
    if not url:
        return []
    try:
        html = await asyncio.wait_for(client.text(url, headers={"Referer": referer} if referer else {}),
                                      timeout=20)
    except Exception:
        return []
    m = _OG_RE.search(html or "")
    if not m:
        return []
    img = m.group(1)
    if img.startswith("//"):
        img = "https:" + img
    if is_junk_url(img):
        return []
    return [Candidate(url=img, source="og", role="other", referer=url)]


async def collect(client, items: list[Item], module: str,
                  figures: dict[str, dict] | None = None) -> list[Candidate]:
    """把「已经有的图」和「还能现取的图」都收进来，按模块补差异化的取图动作。"""
    cands = _from_figures(figures or {}, module, items) + _from_items(items, module)

    if not items:
        return cands
    lead = items[0]
    if module == "paper":
        # HTML 版没拿到「说得清的图」（LaTeXML 未生成 / 老论文 / 只有列表页缩略图）
        # → 回 PDF 首页抠图。**首选是架构图**：HTML 里只有效果/缩略图但没抠到
        # 架构图时，PDF 里很可能藏着 pipeline 大图，也要去挖一趟——穷尽来源
        # 才允许退品牌底图。两条候选都留着，由分数决定谁上封面。
        strong = any(c.role in ("arch", "teaser", "result") for c in cands)
        if not strong or not any(c.role == "arch" for c in cands):
            arxiv_id = str(lead.extra.get("arxiv_id") or "").strip()
            if not arxiv_id:
                # 熵视这类源只给页面链接，arxiv_id 得从 URL 反抠
                m = re.search(r"arxiv\.org/(?:abs|html|pdf)/(\d{4}\.\d{4,5})", lead.url or "")
                arxiv_id = m.group(1) if m else ""
            if arxiv_id:
                base_id = re.sub(r"v\d+$", "", arxiv_id)
                cands += await _pdf_candidates(client, lead, module, "paper-pdf",
                                               f"https://arxiv.org/pdf/{base_id}")
    elif module == "industry":
        repo = str(lead.extra.get("repo") or "")
        if repo:
            cands += await _readme_candidates(client, repo)
        # 项目官网 / 媒体正文的 og:image：README 没头图时的代表图
        home = str(lead.extra.get("homepage") or "")
        if not any(c.role in ("arch", "product", "venue", "body-hero", "logo",
                              "screenshot") for c in cands):
            cands += await _homepage_og(client, home or lead.url)
        # 正文配图兜底（用户铁律：业界稿严禁封面空洞）。
        # enrich 已随正文抓好候选时直接复用（order==0 判 body-hero）；
        # 没有（HackCV/外部原文源、enrich 失败/超时）就现场回抓一次报道页，
        # 「正文第一张核心插图」是 og:image 是 logo、缩略图又太小时的最后一路实景来源。
        strong = {"arch", "product", "venue", "body-hero", "logo", "screenshot"}
        if not any(c.role in strong and c.path for c in cands) \
                and not any(c.role in strong for c in cands):
            try:
                from ..sources.chinese_media import extract_article_figures
                html = await asyncio.wait_for(client.text(lead.url), timeout=20)
                figs = extract_article_figures(html, base=lead.url)
                for i, fig in enumerate(figs):
                    role = classify(str(fig.get("caption") or ""), str(fig.get("url") or ""))
                    if i == 0 and role == "other":
                        role = "body-hero"
                    cands.append(Candidate(
                        url=str(fig["url"]), caption=clean_caption(str(fig.get("caption") or "")),
                        source="media", role=role, referer=lead.url))
            except Exception:
                pass  # 回抓失败不阻断：后面还有救场与品牌底图两层
    elif module == "contest":
        from ..sources import contest_visual

        # ① 官方视觉直采：详情页抓取阶段已顺手抽过（detail["visuals"]）就直接复用，
        #    封面阶段零额外请求；没有（NTIRE / baseline / 详情抓取失败/超时）才现场取一次，
        #    静态 HTML 只有壳时由 contest_visual 走无头渲染（Codabench 这类 SPA）。
        detail = lead.extra.get("contest_detail") or {}
        page = str(lead.extra.get("entry_url") or lead.url or "")
        visuals = list(detail.get("visuals") or [])
        if not visuals and page and lead.extra.get("origin") != "baseline":
            # 详情页已经抓过（detail 非空）却没抽到视觉 → 这页确实没有图，不必再渲染一遍；
            # 只有「详情根本没抓成」时才允许无头兜底一次（SPA 站点的首屏图只有渲染后才有）
            visuals = await contest_visual.page_visuals(
                client, page, allow_headless=not detail, timeout=15.0,
                identity=lead.title)
        cands += _from_visuals(visuals, module, referer=page)
        # 平台官方横幅（Kaggle thumbnailImageUrl / 天池 coverUrl）：这就是平台认定的
        # 赛事代表图，直接按 official 计分——不让「图注判不出角色」把它挡在门外
        if lead.image_url and lead.extra.get("platform"):
            cands.append(Candidate(url=lead.image_url, caption="", source="official",
                                   role="official", referer=lead.url))
        # ② 相关匹配之一：NTIRE 官方报告里的数据集样例 / teaser 图。
        #    enrich_report 已随正文下载抠好候选时直接复用，不再二次拉 PDF。
        report = lead.extra.get("ntire_report") or {}
        has_pdf = any(c.source == "report-pdf" and c.path for c in cands)
        if report.get("pdf_url") and not has_pdf:
            cands += await _pdf_candidates(client, lead, module, "report-pdf",
                                           str(report["pdf_url"]))
        # ② 相关匹配之二：知乎 / CSDN 冠军解读原文里的方案框架图、结果对比图。
        #    仅在前两级（官方图 / 数据集样例 / 框架图 / 现场图）都没着落时才去抓，
        #    省请求，也避免社区图抢掉官方主视觉的位置。
        strong_roles = ("official", "logo", "dataset", "arch", "venue")
        if not any(c.role in strong_roles for c in cands):
            community = [str(row.get("url") or "") for row in (report.get("community") or [])]
            if community:
                cands += _from_visuals(
                    await contest_visual.community_visuals(client, community,
                                                           identity=lead.title),
                    module, referer=lead.url)
        # ③ Datawhale baseline 精读：仓库 README 的横幅 logo / 演示图（logo 走 contain）
        if lead.extra.get("origin") == "baseline":
            m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", lead.url or "")
            if m:
                cands += await _readme_candidates(client, m.group(1))
    return cands


# ---------------------------------------------------------------- 主流程
def rank(cands: list[Candidate], module: str) -> list[Candidate]:
    """过角色白名单、补 fit 版式，并按「下载前可算的分」排序。"""
    allowed = _ACCEPT_ROLES.get(module, set())
    table = _WEIGHTS.get(module, _WEIGHTS["paper"])
    out = []
    seen: set[str] = set()
    for c in cands:
        # 封面必须出自本期主角：一期选用 4 条时，用第 3 条的配图当封面
        # 会出现「文章写 A、封面是 B」，读者第一眼就被带偏
        if c.order != 0:
            continue
        if not c.path and is_junk_url(c.url):
            continue
        if c.role not in allowed:
            continue
        key = c.path or c.url
        if key in seen:                # 同一张图从 images / image_url / figures 重复进来
            continue
        seen.add(key)
        c.fit = "contain" if c.role == "logo" else "cover"
        # c.score 此时只装「条目排序加成」（主角素材 +0.3），下载后再换成实测分
        c.score = (table.get(c.role, 0.5) + _SOURCE_BONUS.get(c.source, 0.0)
                   + (0.5 if c.caption else 0.0) + c.score)
        out.append(c)
    out.sort(key=lambda c: -c.score)
    return out


def _rescue_ok(module: str, cand: Candidate) -> bool:
    """穷尽候选前的救场资格：体检已过关，只按模块卡「图文相配」的底线。

    paper  —— 只救架构/效果/teaser 三大正当角色，dataset/screenshot 差分就真的不配；
    industry —— 用户铁律「严禁封面空洞」：任何体检过关的实景图都好过纯底图，
              但 role=other（完全判不出是什么）必须是横幅比例且够宽，
              否则「图文不搭」比「没图」更伤——那条底线不能为凑数而放。
    contest  —— 用户铁律「尽力而为、不强求」：只救主题正当的角色（官方主视觉/Logo、
              数据集样例、baseline 框架图、赛事现场）。related 与判不出角色的一律不救，
              宁可用品牌底图，也不为凑封面塞一张与赛题无关的图。
    """
    if module == "paper":
        return cand.role in ("arch", "result", "teaser")
    if module == "industry":
        if cand.role != "other":
            return True
        if not cand.width or not cand.height:
            return False
        return cand.width >= 560 and 1.15 <= cand.width / cand.height <= 2.7
    if module == "contest":
        return cand.role in ("official", "logo", "dataset", "arch", "venue")
    return False


def _shape_ok(module: str, cand: Candidate) -> bool:
    """弱角色的「形状底线」：赛事稿的 related 必须是横幅比例。

    竖图 / 方图裁成 2.35:1 会把主体切掉（演讲者头像、实验室合影、赞助商方 logo 都是
    这种形状），封面第一眼就露怯。related 是唯一没有语义证据的角色，
    所以对它卡形状，其余角色交由分数与体检决定。
    """
    if module != "contest" or cand.role != "related":
        return True
    if not cand.width or not cand.height:
        return False
    return cand.width >= 560 and 1.15 <= cand.width / cand.height <= 2.7


async def pick_cover(client, items: list[Item], module: str,
                     figures: dict[str, dict] | None = None) -> CoverPick | None:
    """按模块策略挑一张封面图；挑不到返回 None，调用方走品牌底图。"""
    cands = await collect(client, items, module, figures)
    ranked = rank(cands, module)
    if not ranked:
        return None
    limit = int(_cfg().get("try_limit", 6))
    need = min_score(module)
    notes: list[str] = []
    rescue: Candidate | None = None
    for cand in ranked[:limit]:
        path = cand.path
        if not path or not Path(path).exists():
            path = ""
            for src in [cand.url, *cand.fallbacks]:
                path = await client.download_image(src, referer=cand.referer)
                if path:
                    cand.url = src
                    break
        ok, w, h, reason = _probe(path, cand.role)
        if not ok:
            notes.append(f"{reason}：{cand.url[:60]}")
            continue
        cand.path = path          # 下载产物回填，救场候选才能直接复用
        cand.width, cand.height = w, h
        if not _shape_ok(module, cand):
            notes.append(f"{cand.role} 形状不适配封面（{w}×{h}）：{cand.url[:60]}")
            continue
        cand.score = score(cand, module)
        if cand.score < need:
            # 穷尽候选前不许放弃：体检过关的图哪怕差一点分，也远好过品牌底图
            #（用户铁律：能找到合适的图就严禁空白封面）。
            if _rescue_ok(module, cand):
                if rescue is None or cand.score > rescue.score:
                    rescue = cand
            notes.append(f"{cand.role} 仅 {cand.score:.1f} 分（<{need}）：{cand.url[:60]}")
            continue
        return CoverPick(path=path, url=cand.url, role=cand.role, source=cand.source,
                         caption=cand.caption, score=cand.score, fit=cand.fit,
                         notes=notes)
    if rescue is not None:
        notes.append(f"无候选达到 {need} 分，按「穷尽优先」用体检过关的 "
                     f"{rescue.role}（{rescue.score:.1f} 分）救场上封面")
        return CoverPick(path=rescue.path, url=rescue.url, role=rescue.role,
                         source=rescue.source, caption=rescue.caption,
                         score=rescue.score, fit=rescue.fit, notes=notes)
    return None


def write_meta(pick: CoverPick | None, module: str, out_dir: Path | None = None) -> str:
    """把选图结论落一份 json，方便回头查「这张封面为什么是它」。"""
    out = Path(out_dir or settings.image_dir) / f"cover_{module}.json"
    data: dict[str, Any] = {"module": module, "picked": None, "notes": []}
    if pick:
        data["picked"] = {"url": pick.url, "role": pick.role, "source": pick.source,
                          "caption": pick.caption, "score": round(pick.score, 2),
                          "fit": pick.fit, "path": pick.path}
        data["notes"] = pick.notes
    try:
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return ""
    return str(out)
