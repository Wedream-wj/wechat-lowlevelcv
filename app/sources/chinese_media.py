"""模块 B · 中文 AI 产业媒体直连抓取（量子位 / HackCV）。

为什么不用 RSSHub：公共实例 rsshub.app 在本机网络被代理拦截（实测全部 502），
公众号线路整条断掉，业界稿只能靠 OpenAI / BAIR 这类英文博客撑场面。
这里直接解析站点 HTML，用「标题 + 摘要 + 时间 + 配图」组装 Item，
和 RSS 源走同一套评分与筛选，选中后再回抓正文补事实。

站点改版是常态：解析器只依赖少量稳定 class；
抓不到时返回空列表，由 collect() 把「0 条」记成源告警，绝不静默吞掉。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ..config import topics
from ..filters.scoring import classify_category, score_item
from ..models import Item
from .base import HttpClient, abs_url, strip_html

_TIME_AGO = re.compile(r"(\d+)\s*(分钟|小时|天)前")
_FULL_DATE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
_SHORT_DATE = re.compile(r"^\s*(\d{1,2})[-/月](\d{1,2})\s*$")
_LEAD_NUM = re.compile(r"^\s*\d+\s*[.、)）]\s*")
# 简报里这些栏目不是「产业动态」：论文归学术模块，主编视角是整期综述
_SKIP_SECTIONS = ("arxiv", "主编视角", "参与讨论")


def _cfg(name: str) -> dict:
    return topics.industry.get("chinese_media", {}).get(name, {}) or {}


def parse_cn_time(text: str, now: datetime | None = None) -> datetime | None:
    """解析中文站点的时间写法：刚刚 / N 分钟前 / 昨天 / 10-25 / 2026-10-27。"""
    text = str(text or "").strip()
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    if "刚刚" in text:
        return now
    m = _TIME_AGO.search(text)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta = {"分钟": timedelta(minutes=amount),
                 "小时": timedelta(hours=amount),
                 "天": timedelta(days=amount)}[unit]
        return now - delta
    if "昨天" in text:
        return now - timedelta(days=1)
    m = _FULL_DATE.search(text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc)
        except ValueError:
            return None
    m = _SHORT_DATE.match(text)
    if m:
        try:
            return datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _clean_summary(text: str, limit: int = 800) -> str:
    """简报正文的字段说明（内容/简介/推荐理由/来源）转成紧凑的分隔符。

    直接把「内容 ：xxx 推荐理由 ：yyy」喂给模型，它会照着复述标签；
    统一成「xxx｜推荐理由：yyy」后读起来像一条完整摘要。
    """
    text = strip_html(text, limit)
    text = re.sub(r"^\s*(简介|内容|摘要)\s*[：:]\s*", "", text)
    text = re.sub(r"\s*(推荐理由|来源|热度|领域|链接)\s*[：:]\s*", r"｜\1：", text)
    return text.strip("｜ ")


def _image_src(tag) -> str:
    if tag is None:
        return ""
    return str(tag.get("src") or tag.get("data-src") or tag.get("data-original") or "")


def _finish(item: Item, weight: float) -> Item:
    item.score, item.keywords = score_item(item, topics)
    item.score += weight
    return item


# ------------------------------------------------------------------ 量子位
def parse_qbitai(html: str, base: str = "https://www.qbitai.com/",
                 name: str = "量子位", weight: float = 1.2,
                 limit: int = 30) -> list[Item]:
    """解析量子位首页列表（.picture_text 卡片）。

    首页每条只有一句摘要，够做选题排序；入选后由 enric() 回抓正文，
    否则整篇业界稿只能靠标题猜内容。
    """
    soup = BeautifulSoup(html or "", "lxml")
    items: list[Item] = []
    seen: set[str] = set()
    for block in soup.select(".picture_text"):
        link = (block.select_one(".text_box h4 a")
                or block.select_one(".text_box a")
                or block.select_one(".picture a"))
        title = strip_html(link.get_text(" ", strip=True), 200) if link else ""
        url = abs_url(base, str(link.get("href") or "")) if link else ""
        if not title or not url or url in seen:
            continue
        seen.add(url)
        summary = strip_html(
            " ".join(p.get_text(" ", strip=True) for p in block.select(".text_box p")), 600)
        image = _image_src(block.select_one("img"))
        published = parse_cn_time(
            block.select_one(".time").get_text(" ", strip=True)
            if block.select_one(".time") else "")
        author = ""
        author_node = block.select_one(".author a") or block.select_one(".author")
        if author_node:
            author = strip_html(author_node.get_text(" ", strip=True), 40)

        item = Item(
            module="industry",
            title=title,
            url=url,
            source=name,
            published_at=published,
            summary=summary or title,
            category=classify_category(f"{title} {summary}", topics, "industry"),
            image_url=image,
            images=[image] if image else [],
            extra={"origin": "qbitai", "author": author, "weight": weight},
        )
        items.append(_finish(item, weight))
        if limit and len(items) >= limit:
            break
    return items


def extract_qbitai_article(html: str, limit: int = 3000) -> str:
    """正文抽取：只要 .content .article 里的段落与小标题，丢掉作者栏/版权/广告。"""
    soup = BeautifulSoup(html or "", "lxml")
    art = soup.select_one(".content .article")
    if art is None:
        return ""
    skip = {"article_info", "zhaiyao", "line_font", "pgc-img", "wx_img"}
    parts: list[str] = []
    for node in art.find_all(["p", "h1", "h2", "li"], recursive=True):
        classes = set(node.get("class") or [])
        if classes & skip:
            continue
        if node.find_parent(class_="article_info"):
            continue
        text = strip_html(node.get_text(" ", strip=True), 500)
        if not text or len(text) < 6:
            continue
        if "版权所有" in text or "未经授权" in text:
            continue
        if parts and parts[-1] == text:
            continue  # 图文说明重复出现时只留一次
        parts.append(text)
        if sum(len(p) for p in parts) >= limit:
            break
    return "\n".join(parts)[:limit]


async def fetch_qbitai(client: HttpClient) -> list[Item]:
    cfg = _cfg("qbitai")
    if not cfg.get("enabled", True):
        return []
    url = cfg.get("url") or "https://www.qbitai.com/"
    html = await client.text(url)
    return parse_qbitai(html, base=url, name=cfg.get("name") or "量子位",
                        weight=float(cfg.get("weight", 1.2)),
                        limit=int(cfg.get("max_items", 30)))


# ------------------------------------------------------------------ HackCV
def hackcv_brief_urls(html: str, base: str = "https://hackcv.com/",
                      limit: int = 2) -> list[str]:
    """从首页取最近 N 期「每日简报」链接（周报不算：它是整周综述，不是单条动态）。"""
    soup = BeautifulSoup(html or "", "lxml")
    found: dict[str, str] = {}
    for a in soup.select("a[href]"):
        href = abs_url(base, str(a.get("href") or ""))
        m = re.search(r"/posts/research-brief-(\d{4}-\d{2}-\d{2})/?$", href)
        if m:
            found[href] = m.group(1)
    ordered = sorted(found, key=lambda u: found[u], reverse=True)
    return ordered[:limit]


def _clean_brief_title(text: str) -> str:
    return strip_html(_LEAD_NUM.sub("", str(text or "")), 200)


def parse_hackcv_brief(html: str, url: str, name: str = "HackCV",
                       weight: float = 1.0, limit: int = 40) -> list[Item]:
    """把一期「每日简报」拆成多条独立动态。

    简报本身是整期综述（含 arXiv/开源/资讯三块），直接当一条素材会写成流水账；
    拆成「标题 + 摘要 + 来源链接」后，才能和量子位、GitHub 源一起参与排序。
    """
    soup = BeautifulSoup(html or "", "lxml")
    body = soup.select_one(".article-body")
    if body is None:
        return []
    for junk in body.select("details, .toc, .tag-list, script, style"):
        junk.decompose()
    published = None
    meta = soup.select_one(".article-meta")
    if meta:
        published = parse_cn_time(meta.get_text(" ", strip=True))

    items: list[Item] = []
    section = ""
    for node in body.find_all(["h2", "h3"]):
        if node.name == "h2":
            section = node.get_text(" ", strip=True)
            continue
        heading = section.lower()
        if not section or any(key in heading for key in _SKIP_SECTIONS):
            continue
        title = _clean_brief_title(node.get_text(" ", strip=True))
        if not title or len(title) < 6:
            continue
        para = node.find_next_sibling("p")
        summary = _clean_summary(para.get_text(" ", strip=True)) if para else ""
        link = ""
        if para is not None:
            for a in para.select("a[href]"):
                href = abs_url(url, str(a.get("href") or ""))
                if href.startswith("http") and "hackcv.com" not in href:
                    link = href
                    break
        anchor = str(node.get("id") or "")
        if not link:
            link = f"{url}#{anchor}" if anchor else url
        item = Item(
            module="industry",
            title=title,
            url=link,
            source=name,
            published_at=published,
            summary=summary or title,
            category=classify_category(f"{title} {summary} {section}", topics, "industry"),
            image_url="",
            images=[],
            extra={"origin": "hackcv", "section": section, "brief_url": url,
                   "weight": weight},
        )
        items.append(_finish(item, weight))
        if limit and len(items) >= limit:
            break
    return items


async def fetch_hackcv(client: HttpClient) -> list[Item]:
    cfg = _cfg("hackcv")
    if not cfg.get("enabled", True):
        return []
    url = cfg.get("url") or "https://hackcv.com/"
    html = await client.text(url)
    briefs = hackcv_brief_urls(html, base=url, limit=int(cfg.get("briefs", 2)))
    items: list[Item] = []
    for brief in briefs:
        try:
            page = await client.text(brief)
        except Exception:  # 单期失败不影响其它期
            continue
        items.extend(parse_hackcv_brief(
            page, brief, name=cfg.get("name") or "HackCV",
            weight=float(cfg.get("weight", 1.0)),
            limit=int(cfg.get("max_items", 40))))
    return items[: int(cfg.get("max_items", 40))]


# ------------------------------------------------------------------ 补正文

# 正文图的最小可信宽度：站点装饰条 / 分隔线一般不会超过这个宽度。
# 具体像素级体检（空白/比例/尺寸不足）仍由 cover_select._probe 在下载后做，
# 这里只做「不值得下载」的粗筛，省请求。
_MIN_FIG_WIDTH_HINT = 320


def extract_article_figures(html: str, base: str, max_figs: int = 6) -> list[dict]:
    """从报道正文里抽「封面候选图」，按正文出现顺序返回。

    为什么必须抓正文图：量子位这类媒体的 og:image 是**全站 logo**（实测），
    列表页缩略图只有 200px 上下（体检必挂）——两者都拿不到封面时，
    正文插图（产品图 / 发布会现场 / 架构示意）是唯一符合「图文相配」的来源。

    过滤规则（宁可漏，不可错）：
    - 只要正文容器里的图（.content .article / article / main / .post-content），
      侧栏、评论区、相关推荐的图一律不进封面候选；
    - URL 特征 junk（badge/头像/二维码/logo/icon）直接丢；
    - <img> 带了 width 且小于下限的丢（没写 width 的交给下载后体检）；
    - 图注取 alt / title / figcaption / 相邻说明文字，判角色和审计都用得上。
    返回元素与 cover_select.cover_candidates 的字段约定一致：
    {url, caption, source, role, referer}
    """
    from .base import abs_url

    soup = BeautifulSoup(html or "", "lxml")
    container = None
    for sel in (".content .article", "article", ".post-content", ".article-content",
                "main", ".entry-content"):
        node = soup.select_one(sel)
        if node is not None and node.find("img"):
            container = node
            break
    if container is None:
        return []

    out: list[dict] = []
    for img in container.find_all("img"):
        # 作者栏 / 文章信息区的头像不算正文配图（实测量子位 head.jpg 200×200 在 .article 里）
        if img.find_parent(class_="article_info") or img.find_parent(class_="author"):
            continue
        src = str(img.get("src") or img.get("data-src") or img.get("data-original") or "").strip()
        if not src or src.startswith("data:"):
            continue
        url = abs_url(base, src)
        low = url.lower()
        if any(bad in low for bad in ("logo", "badge", "avatar", "qrcode", "qr-code",
                                      "icon", "favicon", "emoji", "shields.io",
                                      "1x1", "spacer", "loading", "placeholder",
                                      "head.jpg", "weixin", "wechat")):
            continue
        try:
            w_hint = int(str(img.get("width") or "0").rstrip("px"))
        except ValueError:
            w_hint = 0
        if 0 < w_hint < _MIN_FIG_WIDTH_HINT:
            continue
        # 图注：alt → title → figcaption → 父块文本（量子位的图说明常在 <p> 里）
        caption = strip_html(str(img.get("alt") or img.get("title") or ""), 120)
        if not caption:
            fig = img.find_parent("figure")
            if fig is not None:
                cap_node = fig.find("figcaption")
                if cap_node is not None:
                    caption = strip_html(cap_node.get_text(" ", strip=True), 120)
        if not caption:
            holder = img.find_parent(["p", "div"])
            if holder is not None:
                text = strip_html(holder.get_text(" ", strip=True), 120)
                # 说明文字必须「短且非正文段落」：长段是正文，拿它当图注会误导
                if 0 < len(text) <= 60 and "：" not in text[:2]:
                    caption = text
        # 第 0 张 = 报道主视觉（body-hero），其余交由词表判角色
        out.append({
            "url": url, "caption": caption, "source": "media",
            "role": "", "referer": base,
        })
        if len(out) >= max_figs:
            break
    # 角色判定延后到调用方（cover_select.classify），避免循环 import；
    # 只有第一张无条件拿 body-hero——量子位正文首图就是编辑选的主视觉
    for i, fig in enumerate(out):
        fig["order"] = i
    return out


async def enrich(client: HttpClient, items: list, max_items: int = 4) -> int:
    """给入选的量子位条目回抓正文：首页摘要只有一句话，撑不起一篇产业解读。"""
    limit_chars = int(_cfg("qbitai").get("enrich_chars", 3000))
    done = 0
    for item in items:
        if done >= max_items:
            break
        if (item.extra or {}).get("origin") != "qbitai":
            continue
        try:
            html = await client.text(item.url)
        except Exception:
            continue
        text = extract_qbitai_article(html, limit=limit_chars)
        if text:
            item.extra["full_text"] = text
            done += 1
        # 正文配图 → 封面候选（cover_select._from_items 直接消费 cover_candidates）。
        # 量子位 og:image 是全站 logo（实测），列表页缩略图又只有 ~200px，
        # 不抓正文图的话业界稿几乎必然退品牌底图——这是「封面空洞」的主因。
        try:
            figs = extract_article_figures(html, base=item.url)
        except Exception:
            figs = []
        if figs:
            item.extra["cover_candidates"] = figs
        # 列表页缩略图也不丢：留作正文图全挂时的最后一路候选
        if not item.images and item.extra.get("cover_candidates"):
            first = item.extra["cover_candidates"][0]
            item.images = [first["url"]]
            item.image_url = first["url"]
        elif not item.images:
            m = re.search(
                r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']'
                r'[^>]+content=["\']([^"\']+)', html or "")
            # 量子位的 og:image 是全站 logo（实测），不是文章头图——带 logo 字样的
            # 一律不要，宁可退品牌底图，也不能拿站点 logo 当文章封面
            if m and m.group(1).startswith("http") and "logo" not in m.group(1).lower():
                item.images = [m.group(1)]
                if not item.image_url:
                    item.image_url = m.group(1)
    return done
