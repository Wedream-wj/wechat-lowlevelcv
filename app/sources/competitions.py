"""模块 C · 赛事聚合（强时效过滤版）。

五个来源，按可靠性排序：
1. Kaggle 官方 API（结构化最好，需要 KAGGLE_USERNAME / KAGGLE_KEY，免费）；
2. 天池 v3 接口（返回进行中的赛事，自带报名截止 / 比赛截止时间与奖金）；
3. AIcrowd 挑战赛列表页（SSR HTML，解析倒计时徽标里的 UTC 截止时间）；
4. 搜索补全（Serper / Tavily 需 key；无 key 时降级为 Bing 页面解析），
   用来覆盖 CVPR / ECCV workshop 这类没有聚合接口的赛事；
5. 人工策展页（NTIRE / MIPI），页面里提取不到「未来截止时间」的直接跳过。

时效硬规则（痛点 B 的修复核心）：
- 每条素材必须带「截止时间（报名截止与比赛结束中较早者）」或明确的进行中标记；
- 已过期、或截止时间远于 upcoming_days 窗口的，强制过滤；
- 搜索结果里找不到未来日期的，宁可丢弃，也不推荐可能过期的比赛。

统一产出 Item，结构化字段放 extra：platform / prize / deadline / days_left / entry_url。
最终由大模型在 `contest` prompt 里补齐并校验，缺失字段一律写「待官方公布」。
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from ..config import topics
from ..filters.scoring import score_item
from ..models import Item
from .base import HttpClient, strip_html

KAGGLE_API = "https://www.kaggle.com/api/v1/competitions/list"
TIANCHI_DEFAULT_API = "https://tianchi.aliyun.com/v3/proxy/competition/api/race/page"
AICROWD_DEFAULT_URL = "https://www.aicrowd.com/challenges"


# --------------------------------------------------------------- 方向标签
def _contest_category(text: str) -> str:
    """竞赛的赛题方向标签：底层视觉类目优先，判不中再给中性方向，绝不误标。

    categories 词表里的「大模型」这类泛化词，在竞赛语境常指向 LLM / 具身方向
    （如「具身大模型挑战赛」），单独命中不足以判定为「生成式复原」，所以先按
    weak_category_words 剔除这类词再匹配；底层视觉类目全部未命中时，按
    general_tracks 给通用 AI 方向的中性标签，仍然不命中才落「综合赛事」。
    词表都来自 config/topics.yaml 的 competitions 块。
    """
    lowered = (text or "").lower()
    cfg = topics.raw.get("competitions", {})
    weak = {w.lower() for w in cfg.get("weak_category_words", [])}
    cv_hits: dict[str, int] = {}
    for label, words in topics.categories.items():
        hits = sum(1 for w in words if w.lower() in lowered and w.lower() not in weak)
        if hits:
            cv_hits[label] = hits
    if cv_hits:
        return max(cv_hits, key=cv_hits.get)
    gen_hits: dict[str, int] = {}
    for label, words in cfg.get("general_tracks", {}).items():
        hits = sum(1 for w in words if w.lower() in lowered)
        if hits:
            gen_hits[label] = hits
    if gen_hits:
        return max(gen_hits, key=gen_hits.get)
    return "综合赛事"

# ------------------------------------------------------------------ 日期解析
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# 「报名截止：」「Deadline:」这类前缀，剥掉再解析
_LABEL = re.compile(
    r"^\s*(?:报名|参赛|提交|截止|结束|开始|时间|date|deadline|registration|due|ends?|end)[\s:：,，\-—]*",
    re.I,
)
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})"
    r"(?:[T\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?"
    r"\s*(Z|UTC|GMT|[+-]\d{1,2}(?::?\d{2})?)?$",
    re.I,
)
_EN_MD_RE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(\d{4})$")  # March 22, 2026
_EN_DM_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?\s*,?\s+(\d{4})$")   # 22 March 2026
_CN_RE = re.compile(
    r"^(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2})[:：时](\d{1,2})(?:[:：分](\d{1,2})秒?)?)?$"
)

# 自由文本里抽日期候选（网页正文 / 搜索摘要用）
_CANDIDATE_RES = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}(?:[T\s]+\d{1,2}:\d{2}(?::\d{2})?)?(?:\s*(?:UTC|GMT|Z|[+-]\d{1,2}(?::?\d{2})?))?"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}[:：时]\d{1,2}(?:[:：分]\d{1,2}秒?)?)?"),
    re.compile(r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s+\d{4}"),
    re.compile(r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?\s*,?\s+\d{4}"),
)


def parse_deadline(text: str) -> datetime | None:
    """把各种写法的截止时间统一解析成本地（北京时间）naive datetime，失败返回 None。

    支持：ISO（含 Z / UTC / GMT / 时区偏移，统一换算成北京时间）、
    英文月名（March 22, 2026 / 22 March 2026，含 st/nd/rd/th 与缩写月名）、
    中文（2026年1月10日 23:59:59）。只有日期没有时刻的按当天 23:59:59。
    """
    if not text:
        return None
    text = str(text).strip()
    for _ in range(4):  # 剥干净「报名截止：」「Registration End:」这类前缀
        new = _LABEL.sub("", text).strip()
        if new == text:
            break
        text = new
    if not text:
        return None

    dt: datetime | None = None
    m = _ISO_RE.match(text)
    if m:
        hh = int(m.group(4)) if m.group(4) else 23
        mi = int(m.group(5)) if m.group(5) else 59
        ss = int(m.group(6)) if m.group(6) else 59
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), hh, mi, ss)
        except ValueError:
            return None
        tz = m.group(7)
        if tz:
            token = tz.upper()
            if token in ("Z", "UTC", "GMT"):
                offset = timedelta(0)
            else:
                sign = -1 if token[0] == "-" else 1
                body = token[1:].replace(":", "")
                hours = int(body[:-2]) if len(body) > 2 else int(body)
                minutes = int(body[-2:]) if len(body) > 2 else 0
                offset = sign * timedelta(hours=hours, minutes=minutes)
            # 站点给的常是 UTC；统一换算成北京时间再去 tzinfo，和本地 now 直接可比
            dt = dt.replace(tzinfo=timezone(offset)).astimezone(timezone(timedelta(hours=8)))
            dt = dt.replace(tzinfo=None)
    else:
        m_md = _EN_MD_RE.match(text)
        m_dm = _EN_DM_RE.match(text)
        m_zh = _CN_RE.match(text)
        mon: int | None = None
        if m_md:
            mon = _MONTHS.get(m_md.group(1)[:3].lower())
            day, year = int(m_md.group(2)), int(m_md.group(3))
        elif m_dm:
            mon = _MONTHS.get(m_dm.group(2)[:3].lower())
            day, year = int(m_dm.group(1)), int(m_dm.group(3))
        elif not m_zh:
            return None
        if mon is not None:
            try:
                dt = datetime(year, mon, day, 23, 59, 59)
            except ValueError:
                return None
        elif m_zh:
            hh = int(m_zh.group(4)) if m_zh.group(4) else 23
            mi = int(m_zh.group(5)) if m_zh.group(5) else 59
            ss = int(m_zh.group(6)) if m_zh.group(6) else 59
            try:
                dt = datetime(int(m_zh.group(1)), int(m_zh.group(2)), int(m_zh.group(3)), hh, mi, ss)
            except ValueError:
                return None

    if dt is None or not (2020 <= dt.year <= datetime.now().year + 3):
        return None
    return dt


def find_dates(text: str, *, future_only: bool = False) -> list[tuple[datetime, str]]:
    """从自由文本里抽日期候选，返回 (解析结果, 原始串)，按时间升序。

    年份限定在 now-1 ~ now+2：太早的是旧通知，太晚的基本是脏数据。
    """
    if not text:
        return []
    now = datetime.now()
    found: dict[datetime, str] = {}
    for rx in _CANDIDATE_RES:
        for m in rx.finditer(text):
            raw = m.group(0).strip()
            dt = parse_deadline(raw)
            if dt is None:
                continue
            if not (now.year - 1 <= dt.year <= now.year + 2):
                continue
            if future_only and dt <= now:
                continue
            found.setdefault(dt, raw)
    return sorted(found.items())


def filter_active(items: list[Item], upcoming_days: int = 120, now: datetime | None = None) -> list[Item]:
    """强时效过滤：过期 / 超出推荐窗口 / 无截止线索的赛事一律丢弃。

    deadline 的语义是「报名截止与比赛结束中较早者」，由各数据源写 extra["deadline"]（原始串）。
    """
    now = now or datetime.now()
    horizon = now + timedelta(days=upcoming_days)
    expired = windowed = undated = 0
    kept: list[Item] = []
    for item in items:
        raw = str(item.extra.get("deadline") or "")
        dl = parse_deadline(raw) if raw else None
        if dl is not None:
            if dl <= now:
                expired += 1
                continue
            if dl > horizon:
                windowed += 1
                continue
            item.extra["days_left"] = (dl - now).days
            item.score += 2.0
            if item.extra["days_left"] <= 30:
                item.score += 1.0  # 快截止的赛事对读者更紧迫
        elif item.extra.get("confirmed_active"):
            item.score += 1.0  # 无明确截止但确认进行中（如接口标记 isActive 的场次）
        else:
            undated += 1
            continue
        kept.append(item)
    print(
        f"[contest] 时效过滤：过期 {expired} / 超出 {upcoming_days} 天窗口 {windowed} / 无截止线索 {undated}，保留 {len(kept)}",
        flush=True,
    )
    return kept


# ------------------------------------------------------------------ Kaggle
def parse_kaggle(payload: list[dict]) -> list[Item]:
    items: list[Item] = []
    for row in payload or []:
        title = row.get("title") or row.get("ref") or ""
        if not title:
            continue
        ref = row.get("ref", "")
        url = f"https://www.kaggle.com/competitions/{ref}" if ref else row.get("url", "")
        deadline = None
        if row.get("deadline"):
            try:
                deadline = datetime.fromisoformat(str(row["deadline"]).replace("Z", "+00:00"))
            except ValueError:
                deadline = None
        desc = strip_html(row.get("description", "") or "", 600)
        item = Item(
            module="contest",
            title=title,
            url=url,
            source="Kaggle",
            published_at=row.get("enabledDate") and _parse_dt(row["enabledDate"]) or datetime.now(timezone.utc),
            summary=desc,
            category=_contest_category(f"{title} {desc}"),
            image_url=row.get("thumbnailImageUrl", "") or "",
            extra={
                "origin": "kaggle",
                "platform": "Kaggle",
                "prize": row.get("reward", "") or "以官方页面为准",
                "deadline": deadline.isoformat() if deadline else "",
                "entry_url": url,
                "tags": row.get("tags", []),
                "team_count": row.get("teamCount"),
            },
        )
        item.score, item.keywords = score_item(item, topics)
        item.score += 3.0 if deadline and deadline > datetime.now(timezone.utc) else 0.0
        items.append(item)
    return items


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def fetch_kaggle(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("kaggle", {})
    if not cfg.get("enabled", True):
        return []
    user = os.getenv("KAGGLE_USERNAME", "")
    key = os.getenv("KAGGLE_KEY", "")
    if not (user and key):
        return []  # 没配凭证就跳过，不报错
    out: dict[str, Item] = {}
    for term in cfg.get("search_terms", [])[:6]:
        try:
            data = await client.json(
                KAGGLE_API,
                params={"search": term, "page": 1, "pageSize": 20},
                auth=(user, key),
            )
        except Exception:
            continue
        for item in parse_kaggle(data if isinstance(data, list) else []):
            out.setdefault(item.fingerprint, item)
        await asyncio.sleep(0.8)
    return list(out.values())


# ------------------------------------------------------------------ 天池
async def fetch_tianchi(client: HttpClient) -> list[Item]:
    """天池 v3 接口：拉「进行中」的赛事，字段含报名 / 比赛截止时间与奖金。"""
    cfg = topics.raw.get("competitions", {}).get("tianchi", {})
    if not cfg.get("enabled", True):
        return []
    api = cfg.get("api_url", TIANCHI_DEFAULT_API)
    max_pages = int(cfg.get("max_pages", 5))
    items: list[Item] = []
    for page in range(1, max_pages + 1):
        try:
            data = await client.json(
                api, params={"visualTab": "", "raceName": "", "pageNum": page, "isActive": 1}
            )
        except Exception:
            break
        node = data.get("data") if isinstance(data, dict) else None
        rows = (node.get("list") or []) if isinstance(node, dict) else []
        if not rows:
            break
        for row in rows:
            title = strip_html(str(row.get("name") or ""), 160)
            if not title:
                continue
            race_id = row.get("raceId")
            url = (
                f"https://tianchi.aliyun.com/competition/entrance/{race_id}/information"
                if race_id else cfg.get("list_url", "")
            )
            desc = strip_html(str(row.get("introduction") or row.get("highlight") or ""), 600)
            bonus = row.get("bonus") or 0
            signup_end = str(row.get("signupEndTime") or "")
            race_end = str(row.get("raceEndTime") or "")
            # 截止语义：报名截止与比赛结束「较早者」——报名关了进不去，比赛结束了没意义
            dl_signup = parse_deadline(signup_end) if signup_end else None
            dl_end = parse_deadline(race_end) if race_end else None
            if dl_signup and dl_end:
                deadline_raw = signup_end if dl_signup <= dl_end else race_end
            else:
                deadline_raw = signup_end or race_end
            tags = [
                (t.get("tagNameCn") or t.get("tagName") or "")
                for t in (row.get("tagsList") or []) if isinstance(t, dict)
            ]
            item = Item(
                module="contest",
                title=title,
                url=url,
                source="天池",
                published_at=datetime.now(timezone.utc),
                summary=desc,
                category=_contest_category(f"{title} {desc}"),
                image_url=str(row.get("coverUrl") or ""),
                extra={
                    "origin": "tianchi",
                    "platform": "阿里天池",
                    "prize": f"¥{bonus:,}" if bonus else "以官方页面为准",
                    "deadline": deadline_raw,
                    "race_end": race_end,
                    "race_start": str(row.get("raceStartTime") or ""),
                    "entry_url": url,
                    "team_count": row.get("teamCount"),
                    # 接口本身就是 isActive=1 的列表，视为确认进行中
                    "confirmed_active": True,
                },
            )
            item.score, item.keywords = score_item(item, topics)
            item.keywords = list(dict.fromkeys(list(item.keywords) + [t for t in tags if t]))[:12]
            items.append(item)
        await asyncio.sleep(0.8)
    return items


# ------------------------------------------------------------------ AIcrowd
async def fetch_aicrowd(client: HttpClient) -> list[Item]:
    """AIcrowd 挑战赛列表页（SSR HTML）：解析每张卡的 UTC 截止时间与状态。"""
    cfg = topics.raw.get("competitions", {}).get("aicrowd", {})
    if not cfg.get("enabled", True):
        return []
    url = cfg.get("url", AICROWD_DEFAULT_URL)
    try:
        html = await client.text(url)
    except Exception:
        return []
    anchors = list(re.finditer(
        r'<h5 class="card-title">\s*<a href="(/challenges/[^"]+)">\s*(.*?)\s*</a>', html
    ))
    if not anchors:
        return []
    items: list[Item] = []
    for m in anchors:
        name = strip_html(m.group(2), 160)
        if not name:
            continue
        # 每张卡向后取一段，卡与卡互不干扰
        seg = html[m.end(): m.end() + 2500]
        seg_text = strip_html(seg, 500)
        if re.search(r"\b(completed|ended)\b", seg_text, re.I):
            continue  # 已结束的赛题
        dl = re.search(r'title="(\d{4}-\d{2}-\d{2} [\d:]+)\s*UTC"', seg)
        if not dl:
            continue  # 没有截止时间的卡（长期开放/草稿），时效无法保证，跳过
        deadline_raw = f"{dl.group(1)} UTC"
        prize_m = re.search(r'prize-item-cash">\s*(.*?)\s*<', seg)
        prize = re.sub(r"\s*Prize Money\s*$", "", strip_html(prize_m.group(1), 60), flags=re.I) if prize_m else ""
        desc_m = re.search(r'<p class="card-text">\s*(.*?)\s*</p>', seg, re.S)
        desc = strip_html(desc_m.group(1), 300) if desc_m else ""
        cover_m = re.search(r"(https://images\.aicrowd\.com/[^\"'\s]+)", seg)
        link = f"https://www.aicrowd.com{m.group(1)}"
        item = Item(
            module="contest",
            title=name,
            url=link,
            source="AIcrowd",
            published_at=datetime.now(timezone.utc),
            summary=desc or f"{name}：进行中的国际赛事，截止 {deadline_raw}。",
            category=_contest_category(f"{name} {desc}"),
            image_url=cover_m.group(1) if cover_m else "",
            extra={
                "origin": "aicrowd",
                "platform": "AIcrowd",
                "prize": prize or "以官方页面为准",
                "deadline": deadline_raw,
                "entry_url": link,
                # 带倒计时徽标的卡视为确认进行中
                "confirmed_active": True,
            },
        )
        item.score, item.keywords = score_item(item, topics)
        items.append(item)
    return items


# --------------------------------------------------------------- 搜索补全
async def _bing_results(client: HttpClient, query: str) -> list[dict]:
    """免 key 的 Bing 兜底：只有标题 + 摘要，截止时间靠页面文本里的日期线索判断。"""
    from bs4 import BeautifulSoup

    try:
        html = await client.text(f"https://www.bing.com/search?q={quote_plus(query)}")
    except Exception:
        return []
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict] = []
    for li in soup.select("li.b_algo"):
        a = li.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        p = li.select_one("p")
        rows.append({
            "title": a.get_text(" ", strip=True),
            "link": a["href"],
            "snippet": p.get_text(" ", strip=True) if p else "",
        })
    return rows


async def fetch_web_search(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("web_search", {})
    if not cfg.get("enabled", True):
        return []
    serper = os.getenv("SERPER_API_KEY", "")
    tavily = os.getenv("TAVILY_API_KEY", "")
    has_api = bool(serper or tavily)
    if not has_api and not cfg.get("bing_fallback", False):
        return []
    queries = cfg.get("queries", [])
    items: dict[str, Item] = {}

    for query in queries:
        results: list[dict] = []
        if has_api:
            try:
                if serper:
                    data = await client.json(
                        "https://google.serper.dev/search",
                        method="POST",
                        headers={"X-API-KEY": serper, "Content-Type": "application/json"},
                        json={"q": query, "num": 8, "gl": "cn", "hl": "zh-cn"},
                    )
                    results = data.get("organic", []) if isinstance(data, dict) else []
                elif tavily:
                    data = await client.json(
                        "https://api.tavily.com/search",
                        method="POST",
                        json={"api_key": tavily, "query": query, "max_results": 8, "search_depth": "basic"},
                    )
                    results = data.get("results", []) if isinstance(data, dict) else []
            except Exception:
                continue
        else:
            results = await _bing_results(client, query)

        for row in results:
            title = strip_html(row.get("title", ""), 180)
            link = row.get("link") or row.get("url") or ""
            if not title or not link:
                continue
            snippet = strip_html(row.get("snippet") or row.get("content") or "", 500)
            # 搜索结果没有结构化截止时间：标题+摘要里找不到「未来日期」的一律不要，
            # 宁可漏掉，也不推荐可能过期的比赛
            future = find_dates(f"{title} {snippet}", future_only=True)
            if not future:
                continue
            item = Item(
                module="contest",
                title=title,
                url=link,
                source="Web 检索",
                published_at=datetime.now(timezone.utc),
                summary=snippet,
                category=_contest_category(f"{title} {snippet}"),
                extra={
                    "origin": "search",
                    "platform": _guess_platform(link),
                    "prize": "待官方公布",
                    "deadline": future[0][1],
                    "entry_url": link,
                    "query": query,
                },
            )
            item.score, item.keywords = score_item(item, topics)
            item.score += 1.5 if item.category in topics.categories else 0.0
            items.setdefault(item.fingerprint, item)
        await asyncio.sleep(0.5)
    return list(items.values())


def _guess_platform(url: str) -> str:
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url)
    if "kaggle" in host:
        return "Kaggle"
    if "tianchi" in host:
        return "天池"
    if "codalab" in host:
        return "CodaLab"
    if "github" in host:
        return "GitHub"
    return host or "官网"


# --------------------------------------------------------------- 策展页
async def fetch_curated(client: HttpClient) -> list[Item]:
    """人工策展源：必须从官方页面里找到「未来截止时间」才算活着，否则按已过期跳过。"""
    cfg = topics.raw.get("competitions", {}).get("curated", [])
    items: list[Item] = []
    for entry in cfg:
        url = entry.get("url", "")
        if not url:
            continue
        try:
            html = await client.text(url)
        except Exception:
            continue  # 页面未上线（如下一届 404）就静默跳过，官方发布后自动生效
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        page_text = soup.get_text(" ", strip=True)
        future = find_dates(page_text, future_only=True)
        if not future:
            print(f"[contest] 策展页 {entry.get('name', url)} 无未来截止时间，按已过期跳过", flush=True)
            continue
        deadline_raw = future[-1][1]  # 取最晚的：整届赛事（多赛道）的最终截止

        h1 = soup.find(["h1", "h2"])
        page_title = strip_html(h1.get_text(" ", strip=True), 120) if h1 else entry.get("name", url)
        name = entry.get("name", "")
        # 避免出现「NTIRE 2026 · NTIRE 2026」这种重复标题
        title = name if (not page_title or name in page_title) else f"{name} · {page_title}"
        desc_node = soup.find("meta", attrs={"name": "description"})
        desc = strip_html(desc_node.get("content", "") if desc_node else "", 400)
        if not desc or len(desc) < 30:
            desc = entry.get("desc", "") or strip_html(page_text, 600)

        item = Item(
            module="contest",
            title=title,
            url=url,
            source="官方赛事页",
            published_at=datetime.now(timezone.utc),
            summary=desc,
            # 策展源是人工挑选的底层视觉赛事，统一用栏目方向标签
            category="底层视觉",
            extra={
                "origin": "curated",
                "platform": name,
                "prize": "待官方公布",
                "deadline": deadline_raw,
                "entry_url": url,
                "confirmed_active": True,
            },
        )
        item.score, item.keywords = score_item(item, topics)
        item.score += 2.0  # 人工策展的加权只给「页面验证过时效」的存活条目
        items.append(item)
    return items


async def fetch(client: HttpClient) -> list[Item]:
    groups = await asyncio.gather(
        fetch_kaggle(client), fetch_tianchi(client), fetch_aicrowd(client),
        fetch_web_search(client), fetch_curated(client),
        return_exceptions=True,
    )
    merged: dict[str, Item] = {}
    for group in groups:
        if isinstance(group, Exception):
            print(f"[contest] 数据源异常：{type(group).__name__}: {group}", flush=True)
            continue
        for item in group:
            current = merged.get(item.fingerprint)
            # 同一赛事被多个源抓到时，留分数高的那份（通常带更全的 deadline/prize）
            if current is None or item.score > current.score:
                merged[item.fingerprint] = item
    upcoming_days = int(topics.raw.get("competitions", {}).get("filter", {}).get("upcoming_days", 120))
    return filter_active(list(merged.values()), upcoming_days=upcoming_days)
