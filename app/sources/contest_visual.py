"""赛事封面视觉的渐进式检索（官方优先 → 相关匹配 → 尽力而为 → 不强求）。

用户对「赛事竞赛 / Baseline 精读」封面的四级铁律，在这里落成代码：

1. **官方视觉直采**：赛事官网的官方封面图 / 宣传 Banner / 主视觉（Key Visual）/
   官方 Logo——站点自己用 ``og:image`` 声明的那张图，就是它认定的「代表图」；
2. **相关匹配**：官方页正文里的数据集样例图、baseline 框架示意，以及知乎 / CSDN
   冠军解读原文里的方案图（图注能判出角色才算数）；
3. **尽力而为**：以上全无时允许退品牌底图，**不报错、不卡死**——赛事稿缺图不是事故，
   为凑封面卡住出稿才是；
4. **不强求**：绝不为「有张图」去抓一张与赛题无关的图。判不出角色的正文图，
   只有**官方页的第一张**按 related 收（官方页首图通常是主视觉或任务样例），
   其余一律丢弃——「图文不搭」比「没图」更伤读者。

为什么抽图放在「赛事详情页抓取」这一步：赛事页八成是 SPA（Codabench / AI 赛事雷达 /
天池 / 智源），正文本来就要过一遍无头渲染，同一次渲染里把视觉候选一起抽出来，
封面阶段就零额外请求；而且官方 Banner 往往只在首屏渲染后才出现，静态 HTML 里根本没有。

判定顺序（宁可漏，不可错）：
``meta[og:image|twitter:image|image]`` / JSON-LD ``image`` / ``link[rel=image_src]``
→ ``<img>`` 自身或父级 class/id 命中 banner/hero/keyvisual/kv → official；
命中 logo/wordmark/brand → logo（渲染走 contain，不裁切 wordmark）；
其余带图注的交给 ``cover_select.classify`` 判 dataset/arch/venue/result。
"""
from __future__ import annotations

import asyncio
import json
import re

from bs4 import BeautifulSoup

from .base import abs_url, strip_html

# 官方主视觉的容器特征（class / id 归一后精确比对，避免 "discover" 命中 "cover"）
_OFFICIAL_TOKENS = frozenset({
    "banner", "bannerimg", "hero", "heroimage", "keyvisual", "mainvisual", "kv",
    "poster", "cover", "coverimg", "coverimage", "jumbotron", "masthead",
    "headimg", "headerimage", "mainimage",
    "主视觉", "头图", "横幅",
})
_LOGO_TOKENS = frozenset({
    "logo", "logos", "wordmark", "brandmark", "emblem", "brand",
    "徽标", "标识", "标志",
})
# 赞助商 / 合作方区块：那里的 logo 是金主的，不是赛事的身份
_SPONSOR_TOKENS = frozenset({
    "sponsor", "sponsors", "sponsorship", "partner", "partners", "supporter",
    "supports", "coorganizer", "organizer", "organizers",
    "赞助", "合作", "协办", "主办",
})
# 人物照不是赛事视觉：演讲者 / 组织者 / 作者头像
_JUNK_PATH = re.compile(
    r"/(?:speakers?|people|persons?|team|members?|authors?|committee|organizers?|"
    r"portraits?)/|speaker|avatar", re.I)
# 头像 / 二维码 / 社交按钮：alt 就能认出来，不必等下载体检
_JUNK_CAPTION = ("avatar", "profile", "gravatar", "qrcode", "qr code", "二维码",
                 "扫码", "头像", "分享", "share")
# 身份词里这些太泛，不能拿来证明「这个 logo 是本赛事的」
_IDENTITY_STOP = {"www", "com", "org", "net", "edu", "html", "index", "https", "http",
                  "challenge", "competition", "contest", "workshop", "2024", "2025",
                  "2026", "2027"}
_LAZY_ATTRS = ("src", "data-src", "data-original", "data-lazy-src",
               "data-actualsrc", "data-url")
_META_KEYS = ("og:image", "og:image:url", "og:image:secure_url",
              "twitter:image", "twitter:image:src", "image", "thumbnail")
# 站点自动生成的「文字卡」不是视觉：GitHub 的 social preview 就是仓库名排版，
# 拿它当赛事封面等于把标题写两遍
_SKIP_HOSTS = ("opengraph.githubassets.com", "avatars.githubusercontent.com",
               "camo.githubusercontent.com")
_MIN_SIDE_HINT = 300          # img 自带 width 且小于此值的不值得下载


# ---------------------------------------------------------------- 小工具
def _tokens(el) -> set[str]:
    """元素 class/id 归一成词集合。

    既要能认 ``keyvisual``（连写），也要能认 ``hero-banner`` / ``key_visual``（分隔写法）：
    每个 class 既产出「去掉分隔符的整词」，也产出「按分隔符切开的各段」，
    于是 ``hero-banner`` → {herobanner, hero, banner}，两种词表都能命中。
    """
    if el is None:
        return set()
    raw = list(el.get("class") or []) + [str(el.get("id") or "")]
    out: set[str] = set()
    for item in raw:
        text = str(item or "").strip().lower()
        if not text:
            continue
        parts = [p for p in re.split(r"[^0-9a-z\u4e00-\u9fff]+", text) if p]
        out.update(parts)
        if len(parts) > 1:
            out.add("".join(parts))
    return out


def _hint_tokens(img) -> set[str]:
    """自身 + 最多 3 层父级的 class/id：Banner 常写成 ``<div class="banner"><img>``。"""
    out = _tokens(img)
    node = img
    for _ in range(3):
        node = node.parent
        if node is None or getattr(node, "name", "") in ("html", "body"):
            break
        out |= _tokens(node)
    return out


def _img_src(img) -> str:
    for attr in _LAZY_ATTRS:
        val = str(img.get(attr) or "").strip()
        if val and not val.startswith("data:"):
            return val
    srcset = str(img.get("srcset") or "").strip()
    if srcset:
        # srcset 里取第一段（通常是最大或最小的那档，下载后还有体检兜底）
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first:
            return first
    return ""


def _caption(img) -> str:
    """图注：alt → title → figcaption → 相邻短说明。"""
    cap = strip_html(str(img.get("alt") or img.get("title") or ""), 120)
    if cap:
        return cap
    fig = img.find_parent("figure")
    if fig is not None:
        node = fig.find("figcaption")
        if node is not None:
            cap = strip_html(node.get_text(" ", strip=True), 120)
            if cap:
                return cap
    holder = img.find_parent(["p", "div"])
    if holder is not None:
        text = strip_html(holder.get_text(" ", strip=True), 120)
        if 0 < len(text) <= 60:
            return text
    return ""


def _logo_url(url: str) -> bool:
    name = (url or "").lower().rsplit("/", 1)[-1]
    return any(tok in name for tok in ("logo", "wordmark", "brandmark", "emblem"))


def _identity_tokens(base: str, title: str) -> set[str]:
    """本赛事/本页面的身份词：域名与条目标题里的实义词。

    用来回答「这个 logo 是不是**本赛事**的」——赛事官网常在同一页挂一排赞助商
    （OPPO / 快手 / 高校实验室）logo，它们与赛题毫无关系，放上封面等于给金主打广告。
    规则：logo 文件名必须能对上域名或标题里的某个实义词，对不上就不收。
    """
    host = re.sub(r"^https?://", "", base or "").split("/")[0].lower()
    tokens: set[str] = set()
    for blob in (host, (title or "").lower()):
        for part in re.split(r"[^0-9a-z\u4e00-\u9fff]+", blob):
            if len(part) >= 4 and part not in _IDENTITY_STOP:
                tokens.add(part)
    return tokens


def _logo_matches_identity(url: str, identity: set[str]) -> bool:
    if not identity:
        return True                      # 没有身份线索时不做否定判断
    name = (url or "").lower().rsplit("/", 1)[-1]
    compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", name)
    return any(tok in compact for tok in identity)


def _skip(url: str) -> bool:
    from ..render.cover_select import is_junk_url  # 惰性导入：避免与选图模块成环

    if not url or not url.startswith(("http://", "https://")):
        return True
    low = url.lower()
    if any(h in low for h in _SKIP_HOSTS):
        return True
    return is_junk_url(url)


def _meta_images(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    for meta in soup.find_all("meta"):
        key = str(meta.get("property") or meta.get("name") or "").lower()
        if key in _META_KEYS:
            val = str(meta.get("content") or "").strip()
            if val:
                out.append(val)
    for link in soup.find_all("link"):
        if "image_src" in str(link.get("rel") or "").lower():
            val = str(link.get("href") or "").strip()
            if val:
                out.append(val)
    return out


def _jsonld_images(soup: BeautifulSoup, limit: int = 3) -> list[str]:
    """结构化数据里的 image 字段（很多赛事站只在这一处声明主视觉）。"""
    out: list[str] = []
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = [data]
        while stack and len(out) < limit:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                img = node.get("image")
                if isinstance(img, str) and img.startswith(("http", "//")):
                    out.append(img)
                elif isinstance(img, list):
                    out.extend(str(x) for x in img if str(x).startswith(("http", "//")))
                elif isinstance(img, dict) and img.get("url"):
                    out.append(str(img["url"]))
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return out


# ---------------------------------------------------------------- 抽取
def extract_visuals(html: str, base: str, *, source: str = "official",
                    limit: int = 8, identity: str = "") -> list[dict]:
    """从一页 HTML 抽封面候选，按「官方视觉 → Logo → 正文图」顺序返回。

    每项形如 ``{url, caption, role, source, referer, body_no}``：
    role 为空串表示「需要调用方按图注判角色」（cover_select.classify）；
    body_no 是该图在正文里的出现序，0 = 正文第一张（可作 related 兜底）。
    identity 传条目标题：用来确认「这个 logo 是本赛事的」，挡掉赞助商 logo。
    """
    if not html:
        return []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []

    ident = _identity_tokens(base, identity)
    out: list[dict] = []
    seen: set[str] = set()
    state = {"logo": False}

    def add(url: str, caption: str = "", role: str = "", body_no: int | None = None) -> None:
        url = abs_url(base, url)
        if url.startswith("//"):
            url = "https:" + url
        if _skip(url):
            return
        key = url.split("?")[0]
        if key in seen:
            return
        seen.add(key)
        out.append({"url": url, "caption": caption, "role": role, "source": source,
                    "referer": base, "body_no": body_no})

    def add_logo(url: str, caption: str = "") -> None:
        """官方 Logo 只收一张，且必须与本站/本赛事同名（挡赞助商 logo）。"""
        if state["logo"] or not _logo_matches_identity(url, ident):
            return
        state["logo"] = True
        add(url, caption, "logo")

    # ① 站点自己声明的代表图（og:image 等）——官方主视觉的最强信号
    for url in _meta_images(soup) + _jsonld_images(soup):
        if _logo_url(url):
            add_logo(url)
        else:
            add(url, role="official")

    # ② 页面里的 <img>：先挑官方主视觉与 logo，正文图留给下一步
    body: list[tuple[str, str, int]] = []
    for img in soup.find_all("img"):
        src = _img_src(img)
        if not src:
            continue
        url = abs_url(base, src)
        if _skip(url) or _JUNK_PATH.search(url):
            continue
        alt = str(img.get("alt") or img.get("title") or "").lower()
        if any(word in alt for word in _JUNK_CAPTION):
            continue                      # 头像 / 二维码 / 分享按钮：不用等下载就知道没用
        hint = _hint_tokens(img)
        if hint & _SPONSOR_TOKENS:
            continue                      # 赞助商区块：整块跳过，别拿金主 logo 当封面
        is_logo = bool(hint & _LOGO_TOKENS) or _logo_url(url)
        m_w = re.search(r"\d+", str(img.get("width") or ""))
        # 宽度提示只用来粗筛缩略图；logo 本来就小（官网常写 width=200），
        # 它的下限由 _probe 的 logo 专用规则负责，不在这里被误杀
        if not is_logo and m_w and int(m_w.group(0)) < _MIN_SIDE_HINT:
            continue
        caption = _caption(img)
        if hint & _OFFICIAL_TOKENS:
            add(url, caption, "official")
        elif is_logo:
            add_logo(url, caption)
        else:
            body.append((url, caption, len(body)))

    # ③ 正文图：带出出现序（body_no），由调用方决定谁能当 related
    #    ——「正文第一张」是主视觉/任务样例的概率最高，其余判不出角色就丢，
    #    不在这里替调用方做语义判断（角色词表在 cover_select 里）
    for url, caption, idx in body:
        add(url, caption, "", idx)
        if len(out) >= limit:
            break
    return out[:limit]


async def page_visuals(client, url: str, *, allow_headless: bool = True,
                       timeout: float = 25.0, identity: str = "") -> list[dict]:
    """赛事页的官方视觉：静态 HTML 抽不到时用无头渲染再取一次（SPA 站点）。

    每一步都吞异常：赛事页 403 / 蜜罐 / 超时都只意味着「这页没有视觉」，
    绝不能把出稿流程带崩。
    """
    if not url:
        return []
    html = ""
    try:
        html = await asyncio.wait_for(client.text(url), timeout=timeout)
    except Exception:
        html = ""
    visuals = extract_visuals(html, url, source="official", identity=identity)

    strong = any(v["role"] in ("official", "logo") for v in visuals)
    # 静态 HTML 只有壳（SPA）或一张官方视觉都没有 → 交给无头浏览器渲染首屏
    shell = len(html or "") < 6000 or re.search(r"enable javascript|without javascript",
                                                html or "", re.I)
    if allow_headless and not strong and (shell or not visuals):
        try:
            from . import headless

            dom = await asyncio.wait_for(
                headless.render_dom(url, timeout=40.0, virtual_ms=9000), timeout=50.0)
        except Exception:
            dom = ""
        if dom:
            rendered = extract_visuals(dom, url, source="official", identity=identity)
            if len(rendered) > len(visuals):
                visuals = rendered
    return visuals


async def community_visuals(client, urls: list[str], *, per_page: int = 2,
                            limit: int = 3, identity: str = "") -> list[dict]:
    """冠军解读原文（知乎 / CSDN）里的配图：第二级「相关生态图」的来源。

    只取正文前两张——社区文章的图多是方案框架图或结果对比，与赛题同源；
    失败一律忽略：这是「尽力而为」的一层，拿不到就交给品牌底图。
    """
    if not urls:
        return []
    out: list[dict] = []
    for url in urls[:2]:
        if not url or not url.startswith("http"):
            continue
        try:
            html = await asyncio.wait_for(client.text(url), timeout=20.0)
        except Exception:
            continue
        figs = extract_visuals(html, url, source="media", limit=per_page,
                               identity=identity)
        out.extend(figs)
        if len(out) >= limit:
            break
    return out[:limit]
