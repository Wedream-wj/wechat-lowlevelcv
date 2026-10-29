"""模块 C · 赛事聚合（强时效过滤版）。

七个赛事来源 + 一个学习资源源，按可靠性排序：
1. Kaggle 官方 API（结构化最好，需要 KAGGLE_USERNAME / KAGGLE_KEY，免费）；
2. 天池 v3 接口（返回进行中的赛事，自带报名截止 / 比赛截止时间与奖金）；
3. AIcrowd 挑战赛列表页（SSR HTML，解析倒计时徽标里的 UTC 截止时间）；
4. 搜索补全（Serper / Tavily 需 key；无 key 时降级为 Bing 页面解析），
   用来覆盖 CVPR / ECCV workshop 这类没有聚合接口的赛事；
5. 人工策展页（NTIRE / MIPI），页面里提取不到「未来截止时间」的直接跳过；
6. DataCastle 在办赛事列表（SPA 背后的 /v3/api JSON，无截止时间，靠 confirmed_active 放行）；
7. Coggle 赛事聚合页（SSG HTML，解析日期区间与奖金；部分网络不可达时优雅降级）；
8. Datawhale「竞赛Baseline」仓库（学习资源，非赛事，单独限量拼接，不参与时效过滤）。

时效硬规则（痛点 B 的修复核心）：
- 每条素材必须带「截止时间（报名截止与比赛结束中较早者）」或明确的进行中标记；
- 已过期、或截止时间远于 upcoming_days 窗口的，强制过滤；
- 搜索结果里找不到未来日期的，宁可丢弃，也不推荐可能过期的比赛。

统一产出 Item，结构化字段放 extra：platform / prize / deadline / days_left / entry_url。
最终由大模型在 `contest` prompt 里补齐并校验，缺失字段一律写「待官方公布」。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, quote_plus

from ..config import topics
from ..filters.scoring import score_item
from ..models import Item
from . import headless
from .base import HttpClient, strip_html

try:  # 解析官方页 / 静态列表用；缺依赖时不至于整模块导入失败
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None  # type: ignore[assignment]

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


# 只有荣誉/积分的「练习赛」：没有奖金、参赛规模又小，撑不起一篇精读
_PLACEHOLDER_PRIZES = {"kudos", "knowledge", "swag", "points", "badge", "certificate",
                       "honor", "honour", "证书", "荣誉"}


def _is_low_value(item: Item) -> bool:
    """课堂练习赛识别：奖金只是 Kudos/积分，且参赛队伍不到 200 支。"""
    prize = str(item.extra.get("prize") or "").strip().lower()
    if prize not in _PLACEHOLDER_PRIZES:
        return False
    try:
        teams = int(item.extra.get("team_count") or 0)
    except (TypeError, ValueError):
        teams = 0
    return teams < 200


def filter_active(items: list[Item], upcoming_days: int = 120, now: datetime | None = None) -> list[Item]:
    """强时效过滤：过期 / 超出推荐窗口 / 无截止线索的赛事一律丢弃。

    deadline 的语义是「报名截止与比赛结束中较早者」，由各数据源写 extra["deadline"]（原始串）。
    """
    now = now or datetime.now()
    horizon = now + timedelta(days=upcoming_days)
    expired = windowed = undated = lowvalue = 0
    kept: list[Item] = []
    for item in items:
        if _is_low_value(item):
            lowvalue += 1
            continue
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
        f"[contest] 时效过滤：过期 {expired} / 超出 {upcoming_days} 天窗口 {windowed} / "
        f"无截止线索 {undated} / 练习赛 {lowvalue}，保留 {len(kept)}",
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
        if ref.startswith("http"):  # 新版 API 的 ref 可能已是完整 URL
            url = ref
        elif ref:
            url = f"https://www.kaggle.com/competitions/{ref}"
        else:
            url = row.get("url", "")
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
        # 质量分：Kaggle 里大量课堂练习赛（奖金 Kudos、队伍 1-20 支），
        # 关键词一样命中，但撑不起一篇精读。真金白银 + 真实参赛规模才算值得写。
        prize_txt = str(item.extra.get("prize") or "")
        if any(cur in prize_txt for cur in "¥￥$"):
            item.score += 2.0
        elif prize_txt and prize_txt.lower() not in ("kudos", "knowledge", "swag", "points"):
            item.score += 1.0
        teams = int(row.get("teamCount") or 0)
        if teams >= 200:
            item.score += 1.5
        elif teams <= 10:
            item.score -= 1.5
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
    # 新版 KGAT_ token 只认 Bearer；旧版 kaggle.json 密钥走 Basic
    if key.startswith("KGAT_"):
        kwargs: dict = {"headers": {"Authorization": f"Bearer {key}"}}
    else:
        kwargs = {"auth": (user, key)}
    for term in cfg.get("search_terms", [])[:6]:
        try:
            data = await client.json(
                KAGGLE_API,
                params={"search": term, "page": 1, "pageSize": 20},
                **kwargs,
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
            teams = int(row.get("teamCount") or 0)
            if teams >= 200:
                item.score += 1.5  # 有真实竞争规模，才撑得起「破局思路」这一节
            elif bonus:
                item.score += 0.8
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


# --------------------------------------------------------------- DataCastle
DATACASTLE_API = "https://challenge.datacastle.cn/v3/api/common/cmpt/cmptIndex.json"


async def fetch_datacastle(client: HttpClient) -> list[Item]:
    """DataCastle（DC 竞赛）在办赛事。

    页面 cmptlist.html 是 SPA，列表来自 /v3/api/common/cmpt/cmptIndex.json 的
    data.activeList——能进这个列表的就是在办赛事，因此 deadline 空缺时用
    confirmed_active=True 让时效过滤放行。列表接口不含奖金/截止时间，
    按纪律留空由成稿写「待官方公布」。
    """
    cfg = topics.raw.get("competitions", {}).get("datacastle", {})
    if not cfg.get("enabled", True):
        return []
    data = await client.json(DATACASTLE_API)
    rows = (data.get("data") or {}).get("activeList") or []
    items: list[Item] = []
    for row in rows:
        name = strip_html(row.get("name", "") or "", 100)
        if not name:
            continue
        cid = row.get("id")
        url = (f"https://challenge.datacastle.cn/v3/cmptDetail.html?cmptId={cid}"
               if cid else "https://challenge.datacastle.cn/v3/cmptlist.html")
        labels = "、".join(
            x for x in (row.get("industry_label"), row.get("technology_label"), row.get("type"))
            if x
        )
        item = Item(
            module="contest",
            title=name,
            url=url,
            source="DataCastle",
            published_at=datetime.now(timezone.utc),
            summary=strip_html(labels or "DataCastle 在办赛事", 300),
            category=_contest_category(f"{name} {labels}"),
            image_url=row.get("image") or "",
            extra={
                "origin": "datacastle",
                "platform": "DataCastle",
                "prize": "待官方公布",
                "deadline": "",
                "entry_url": url,
                "confirmed_active": True,  # activeList 本身就是在办列表
                "tags": [row.get("type")] if row.get("type") else [],
            },
        )
        item.score, item.keywords = score_item(item, topics)
        items.append(item)
    return items


# --------------------------------------------------------------- Coggle
COGGLE_DEFAULT_URL = "https://coggle.club/?platform=xfyun"
_CN_DATE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def _cn_date_to_dt(y: str, m: str, d: str) -> datetime:
    return datetime(int(y), int(m), int(d), 23, 59, 59, tzinfo=timezone.utc)


def _text_between(start_el, end_el, limit: int = 1200) -> str:
    """取 start_el 到 end_el 之间的可见文本（不含 end_el）。"""
    from bs4 import BeautifulSoup

    parts: list[str] = []
    for el in start_el.find_all_next():
        if el is end_el:
            break
        if el.name in ("script", "style"):
            continue
        # 只取「叶子」文本，避免父节点重复拼接整段子树
        if el.find() is None:
            parts.append(el.get_text(" ", strip=True))
        if sum(len(p) for p in parts) > limit:
            break
    return strip_html(" ".join(parts), limit)


async def fetch_coggle(client: HttpClient) -> list[Item]:
    """Coggle 数据科学（讯飞等平台的赛事聚合页）。

    站点是 SSG 渲染，条目形如 <h3><a href="/competitions/xxx">名称</a></h3>，
    后文有「已结束」徽标、「YYYY年M月D日→YYYY年M月D日」区间与奖金行。
    本站在部分网络不可达（DNS 级阻断），失败时返回空由上层记告警，不阻塞管线。
    """
    cfg = topics.raw.get("competitions", {}).get("coggle", {})
    if not cfg.get("enabled", True):
        return []
    url = cfg.get("url", COGGLE_DEFAULT_URL)
    try:
        html = await client.text(url)
    except Exception:
        return []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    items: list[Item] = []
    now = datetime.now(timezone.utc)
    anchors = [a for a in soup.find_all("a", href=re.compile(r"/competitions/[^/]+"))
               if strip_html(a.get_text(" ", strip=True), 100)]
    for idx, a in enumerate(anchors):
        name = strip_html(a.get_text(" ", strip=True), 100)
        href = a["href"]
        if href.startswith("/"):
            href = "https://coggle.club" + href
        # 条目文本窗口：从本锚点到下一个赛事锚点之间的文本（含日期区间/奖金/已结束徽标）
        end_el = anchors[idx + 1] if idx + 1 < len(anchors) else None
        window_text = _text_between(a, end_el)
        if "已结束" in window_text:
            continue  # 只保留未结束的
        dates = _CN_DATE.findall(window_text)
        deadline = ""
        if len(dates) >= 2:
            end = _cn_date_to_dt(*dates[1])
            if end <= now:
                continue
            deadline = f"{dates[1][0]}-{dates[1][1]}-{dates[1][2]}"
        desc = ""
        for seg in window_text.split(name, 1)[1:] or [window_text]:
            if len(seg) > 40:
                desc = seg[:600]
                break
        prize_m = re.search(r"(一等奖|二等奖|奖金|冠名|[0-9,]+\s*(?:CNY|RMB|USD))", window_text)
        if prize_m:
            prize = window_text[prize_m.start():prize_m.end() + 60].split("·")[0].strip()
        else:
            prize = "待官方公布"
        item = Item(
            module="contest",
            title=name,
            url=href,
            source="Coggle",
            published_at=now,
            summary=strip_html(desc or "讯飞等平台的赛事聚合条目", 500),
            category=_contest_category(f"{name} {desc}"),
            extra={
                "origin": "coggle",
                "platform": "讯飞/Coggle",
                "prize": strip_html(prize, 60),
                "deadline": deadline,
                "entry_url": href,
                # 页面能抓到就视为在办；无日期时靠该标记过时效过滤
                "confirmed_active": not deadline,
            },
        )
        item.score, item.keywords = score_item(item, topics)
        items.append(item)
    # 同名去重（首页与聚合页可能重复）
    seen: set[str] = set()
    unique = []
    for it in items:
        if it.fingerprint in seen:
            continue
        seen.add(it.fingerprint)
        unique.append(it)
    return unique


# --------------------------------------------------------------- Datawhale baseline
BASELINE_LIST_API = ("https://api.github.com/repos/datawhalechina/"
                     "competition-baseline/contents/competition")


def github_headers() -> dict:
    """api.github.com 认证头。

    匿名调用限额 60 次/小时——baseline 精读一期要打 6~8 个 API（目录清单 +
    README + 代码文件 + 检索佐证），反复调试时极易打满，打满后源静默返回 0 条。
    在 .env 配置 GITHUB_TOKEN（GitHub → Settings → Developer settings →
    Personal access tokens，无需任何权限勾选的只读 classic token 即可）后
    配额提升到 5000 次/小时。
    """
    token = (os.getenv("GITHUB_TOKEN", "").strip()
             or os.getenv("GH_TOKEN", "").strip())
    return {"Authorization": f"Bearer {token}"} if token else {}

# EDA notebook 里值得抽进成稿的代码模式（数据洞察结论的来源）
_EDA_CELL_KEYWORDS = re.compile(
    r"(describe|value_counts|nunique|groupby|corr\(|isnull|isna|head\(|tail\(|info\(|"
    r"unique\(|plot|hist|bar|scatter|countplot|heatmap|pivot|skew|quantile)",
    re.I,
)

# 目录名 → 赛事背景推断（README 缺失时给成稿兜底方向感）
_PLATFORM_HINTS = [
    ("dcic", "数字中国创新大赛（DCIC）"),
    ("数字中国", "数字中国创新大赛（DCIC）"),
    ("tianchi", "阿里天池"),
    ("kaggle", "Kaggle"),
    ("aicrowd", "AIcrowd"),
    ("datacastle", "DataCastle 竞赛"),
    ("ccf", "CCF 系列赛事"),
    ("讯飞", "讯飞开放平台赛事"),
    ("iFLYTEK", "讯飞开放平台赛事"),
]

# 目录名 → 任务形态推断
_TASK_HINTS = [
    ("classif", "分类任务"), ("class", "分类任务"), ("reco", "分类/识别任务"),
    ("recogn", "分类/识别任务"), ("regress", "回归任务"), ("detection", "检测任务"),
    ("segment", "分割任务"), ("recommend", "推荐任务"), ("nlp", "文本任务"),
    ("ocr", "文字识别任务"), ("超分", "超分辨率任务"), ("去噪", "去噪任务"),
    ("低光", "低光增强任务"), ("去雾", "图像去雾任务"), ("去雨", "图像去雨任务"),
]

# 单个代码文件的最大下载体积（base64 前的原始字节数），防止巨型 notebook 拖垮抓取
_CODE_FILE_MAX_BYTES = 8_000_000


def _paradigm_hint(folder: str, files: list[str]) -> str:
    """按目录名与文件形态生成范式推演提示，README 过短时供成稿兜底。

    这里只做「通用范式级」推断（赛事背景、任务形态、常见指标与 CV 打法），
    不含任何具体数字；具体事实仍以素材与模型的专业推断句式呈现。
    """
    low = folder.lower()
    platform = next((label for kw, label in _PLATFORM_HINTS if kw in low), "")
    task = next((label for kw, label in _TASK_HINTS if kw in low), "")
    nb = sum(1 for f in files if f.endswith(".ipynb"))
    py = sum(1 for f in files if f.endswith(".py"))
    parts: list[str] = []
    if platform:
        parts.append(f"赛事背景：{platform}系列")
    if task:
        parts.append(f"任务形态：{task}")
    if nb and not py:
        parts.append(
            "仓库定位：由 EDA notebook 构成的「数据洞察型 baseline」——价值集中在数据分布结论，"
            "模型侧从零起步，为后续迭代留出极大的调优空间"
        )
    parts.append(
        "通用范式提示：结构化数据赛道通常围绕 AUC / F1 / RMSE 之一展开评测；"
        "验证切分上，分类用 StratifiedKFold、带主体/分组字段用 GroupKFold、"
        "含时间字段用时序滚动切分；起步模型首选 LightGBM / XGBoost / CatBoost 三件套，"
        "特征工程优先做 Target Encoding、聚合统计、交叉特征与时序滑动窗口"
    )
    return "；".join(parts)


def _ipynb_code(text: str, limit: int = 2200) -> str:
    """notebook JSON → code cell 文本。

    优先抽取含 EDA 模式的 cell（describe/value_counts/groupby/可视化…）——
    它们是数据分布结论的直接来源；巨型 notebook 的体积基本都在 output 里，
    这里只取 code cell，天然绕开了输出噪声。
    """
    try:
        nb = json.loads(text)
    except Exception:
        return text[:limit]
    cells: list[str] = []
    for cell in nb.get("cells") or []:
        if cell.get("cell_type") != "code":
            continue
        src = cell.get("source") or []
        code = ("".join(src) if isinstance(src, list) else str(src)).strip()
        if code:
            cells.append(code)
    if not cells:
        return ""
    interesting = [c for c in cells if _EDA_CELL_KEYWORDS.search(c)]
    # 首个 cell 通常是数据读取入口，与 EDA cell 去重后按序拼接
    chosen = list(dict.fromkeys(cells[:1] + interesting)) or cells
    out: list[str] = []
    total = 0
    for c in chosen:
        if total >= limit:
            break
        seg = c[: limit - total]
        out.append(seg)
        total += len(seg) + 2
    return "\n\n".join(out)


def _cjk_bigrams(text: str) -> set[str]:
    """取文本中的中文二字串集合，用于检索结果的相关性过滤。"""
    cjk = re.sub(r"[^\u4e00-\u9fff]+", "", text or "")
    if len(cjk) < 2:
        return {cjk} if cjk else set()
    return {cjk[i:i + 2] for i in range(len(cjk) - 1)}


async def search_competition_context(client: HttpClient, folder: str, max_results: int = 4) -> list[dict]:
    """联网检索赛事官方背景：元数据不全时把「模型推测」升级为「检索查证」。

    渠道降级链：Serper → Tavily（需 key）→ 天池官方接口 → GitHub 仓库搜索 → Bing。
    天池接口与 GitHub 搜索免 key 且结构化，权威性高于通用网页搜索；Bing 仅作最后兜底。
    全部结果过一道中文二字串相关性过滤，挡住「2021年 百度百科」这类与赛题无关的召回。
    返回 [{title, snippet, url, source}]；全部失败返回 []，不阻塞管线。
    """
    # 长划线/斜杠等分隔符换成空格，保留普通连字符（多为赛题名的一部分）
    core = re.sub(r"[—–_/]+", " ", folder).strip()
    segments = sorted((s.strip() for s in core.split() if len(s) >= 4), key=len, reverse=True)
    phrase = segments[0] if segments else core
    core_bigrams = _cjk_bigrams(core)
    queries = [f"{core} 竞赛 赛题 数据 说明", f'"{phrase}" 赛题 官方']
    serper = os.getenv("SERPER_API_KEY", "")
    tavily = os.getenv("TAVILY_API_KEY", "")

    def _relevant(text: str, need: int = 2) -> bool:
        if not core_bigrams:
            return True
        return len(_cjk_bigrams(text) & core_bigrams) >= need

    def _collect(rows: list[dict], source: str) -> list[dict]:
        out: list[dict] = []
        for row in rows:
            title = strip_html(row.get("title", ""), 120)
            snippet = strip_html(row.get("snippet") or row.get("content") or "", 400)
            link = row.get("link") or row.get("url") or ""
            if title and link and _relevant(f"{title} {snippet}"):
                out.append({"title": title, "snippet": snippet, "url": link, "source": source})
        return out

    async def _serper(q: str) -> list[dict]:
        data = await client.json(
            "https://google.serper.dev/search",
            method="POST",
            headers={"X-API-KEY": serper, "Content-Type": "application/json"},
            json={"q": q, "num": 6, "gl": "cn", "hl": "zh-cn"},
        )
        return _collect(data.get("organic", []) if isinstance(data, dict) else [], "Serper")

    async def _tavily(q: str) -> list[dict]:
        data = await client.json(
            "https://api.tavily.com/search",
            method="POST",
            json={"api_key": tavily, "query": q, "max_results": 6, "search_depth": "basic"},
        )
        return _collect(data.get("results", []) if isinstance(data, dict) else [], "Tavily")

    async def _tianchi() -> list[dict]:
        """天池官方赛题接口：raceName 模糊检索，返回官方赛题名与介绍（结构化、最权威）。

        接口的模糊匹配对后缀敏感（「阿里云供应链大赛」查不到、「供应链大赛」能查到），
        因此对赛题名做渐进缩短的后缀阶梯尝试，命中即止。
        """
        out: list[dict] = []
        kws: list[str] = []
        for seg in segments[:2]:
            base = re.sub(r"^\d{4}", "", seg)
            kws += [base, base[2:], base[4:], base[6:]]
        kws = [k for k in dict.fromkeys(kws) if len(k) >= 3][:5]
        for kw in kws:
            try:
                data = await client.json(
                    "https://tianchi.aliyun.com/v3/proxy/competition/api/race/page",
                    params={"raceName": kw, "pageNum": 1, "isActive": ""},
                )
            except Exception:
                continue
            rows = ((data.get("data") or {}).get("list") or []) if isinstance(data, dict) else []
            for row in rows:
                name = strip_html(str(row.get("name") or ""), 120)
                intro = strip_html(str(row.get("introduction") or row.get("highlight") or ""), 400)
                rid = row.get("raceId")
                url = (f"https://tianchi.aliyun.com/competition/entrance/{rid}/information"
                       if rid else "https://tianchi.aliyun.com/competition/gameList/activeList")
                if name and _relevant(f"{name} {intro}"):
                    out.append({"title": name, "snippet": intro, "url": url,
                                "source": "天池官方接口"})
            if out:
                break
        return out

    async def _github_repos() -> list[dict]:
        """GitHub 仓库搜索：参赛者/主办方仓库的描述是赛题全名与任务的独立佐证。"""
        data = await client.json(
            "https://api.github.com/search/repositories",
            params={"q": core, "per_page": 6},
            headers=github_headers(),
        )
        items = data.get("items") or [] if isinstance(data, dict) else []
        out: list[dict] = []
        for r in items:
            name = strip_html(r.get("full_name") or "", 120)
            desc = strip_html(r.get("description") or "", 400)
            if name and _relevant(f"{name} {desc}"):
                out.append({"title": f"GitHub · {name}", "snippet": desc,
                            "url": r.get("html_url") or "", "source": "GitHub 仓库搜索"})
        return out

    results: list[dict] = []
    seen_urls: set[str] = set()

    def _merge(rows: list[dict]) -> None:
        for r in rows:
            if r.get("url") and r["url"] not in seen_urls:
                seen_urls.add(r["url"])
                results.append(r)

    for q in queries:
        if serper:
            try:
                _merge(await _serper(q))
            except Exception:
                pass
        if not results and tavily:
            try:
                _merge(await _tavily(q))
            except Exception:
                pass
        if not results:
            try:
                _merge(await _tianchi())
            except Exception:
                pass
        # GitHub 仓库描述含完整赛题任务名，与官方介绍互补，始终合并作结构化佐证
        try:
            _merge(await _github_repos())
        except Exception:
            pass
        if not results:
            try:
                _merge(_collect(await _bing_results(client, q), "Bing"))
            except Exception:
                pass
        if results:
            break
    return results[:max_results]


# --------------------------------------------------- 单篇精读：赛事详情核验
# 「报名已截止」这类**状态描述**才算关闭；「报名截止：2026-12-31」是字段标签，不能误判
_CLOSED_STATES = ("报名已截止", "报名已结束", "报名停止", "已停止报名", "报名通道已关闭",
                  "registration closed", "registration is closed", "submissions closed")
_METRIC_HINTS = ("指标", "评测", "评分", "评分标准", "评估", "auc", "f1", "psnr", "ssim",
                 "map", "rmse", "mae", "准确率", "召回", "精度", "leaderboard", "score")
# 「报名截止 2026-10-26」这类写法：抽日期用来判断报名口是否已经关了
_REG_DEADLINE_RE = re.compile(
    r"报名(?:截止|结束|关闭)[^\d]{0,8}(\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)")


def _snippet_lines(text: str, hints: tuple[str, ...], limit: int = 6) -> list[str]:
    """按句切分，挑出含关键词的句子，给模型做「评测口径 / 规则」的原始依据。"""
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?；;])\s*|\n+", text)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        s = part.strip()
        if not (12 <= len(s) <= 220) or s in seen:
            continue
        low = s.lower()
        if any(h in low for h in hints):
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out


def registration_closed(text: str, now: datetime | None = None) -> bool:
    """报名口是否已关。两条证据取并集：

    1. 页面出现「报名已结束 / 报名已截止 / registration closed」这类**状态描述**；
    2. 页面「报名截止 <日期>」里最早的日期已经过去（赛事页常同时列初赛与决赛两套日期，
       报名口只认最早那个）。

    只看决赛日期在未来就判定「还能报名」是错的——实测天池某赛 10-26 关报名、11-16 才决赛。
    """
    if not text:
        return False
    low = text.lower()
    if any(w in low for w in _CLOSED_STATES):
        return True
    now = now or datetime.now()
    regs = [d for d in (parse_deadline(m) for m in _REG_DEADLINE_RE.findall(text)) if d]
    return bool(regs and min(regs) <= now)


# 页面自带的报名控件是最硬的一条证据：天池在报名开着时会直接渲染「报名剩 03 日 14 时」，
# 而同一页的「2026-05-31 报名与资格赛结束」讲的是**资格赛阶段**，不是报名口。
# 只认带计数的倒计时（动态渲染，不可能是模板残留），「立即报名」这种按钮文案不算。
_REG_COUNTDOWN_RE = re.compile(r"报名剩\s*\d+\s*日(?:\s*\d+\s*时)?|距报名(?:结束|截止)")


def registration_status(text: str, now: datetime | None = None) -> tuple[bool, str]:
    """返回 (报名是否已截止, 页面自带状态原句)。

    优先级：页面倒计时 > 「报名已结束」状态词 / 报名截止日期。
    倒计时一旦出现就是报名口开着，否则模型会把资格赛结束日期写成「报名已结束」。
    """
    if not text:
        return False, ""
    m = _REG_COUNTDOWN_RE.search(text)
    if m:
        return False, m.group(0).strip()
    return registration_closed(text, now), ""


async def fetch_contest_detail(client: HttpClient, item: Item) -> dict:
    """拉单选赛事的官方页面，抽出时间线 / 奖金 / 评测口径，并判定报名是否还开着。

    这一步是「单篇深度精读」的素材底座：只有标题 + 一句摘要，七个维度里至少四个
    （数据、指标、周期、奖项）会变成模型编造。顺序是：普通请求 → 无头渲染兜底。
    """
    detail = dict(item.extra.get("contest_detail") or {})
    url = str(item.extra.get("entry_url") or item.url or "")
    text = ""
    if url:
        try:
            body = await client.text(url)
        except Exception:
            body = ""
        if body and not headless.looks_like_honeypot(body):
            try:
                soup = BeautifulSoup(body, "lxml")
                text = soup.get_text(" ", strip=True)
            except Exception:
                text = ""
        # 赛事页八成是 SPA（天池 / Codabench / 天智杯都一样）：静态 HTML 只有壳，
        # 650 字左右时直接判为「没拿到正文」，走无头渲染再取一次
        if len(text) < 1200:
            try:
                rendered = await headless.render_text(url, timeout=40.0, virtual_ms=12000)
            except Exception:
                rendered = ""
            if len(rendered) > len(text):
                text = rendered
    text = strip_html(text, 9000)
    detail["page_excerpt"] = text[:3000]
    detail["timeline"] = [raw for _, raw in find_dates(text, future_only=True)][:8]
    detail["prize_hint"] = _snippet_lines(text, ("奖金", "奖池", "prize", "usd", "$", "万元"), 3)
    detail["metrics_hint"] = _snippet_lines(text, _METRIC_HINTS, 6)
    closed, reg_hint = registration_status(text)
    detail["registration_closed"] = closed
    detail["registration_hint"] = reg_hint
    # 列表接口不给截止时间的源（Codabench）：用官方页里最近的未来日期补一个下限
    if not item.extra.get("deadline") and detail["timeline"]:
        item.extra["deadline"] = detail["timeline"][0]
    detail["fetched"] = bool(text)
    return detail


async def fetch_baseline(client: HttpClient) -> list[Item]:
    """Datawhale「竞赛Baseline」开源仓库：往届赛题的 baseline 复现合集。
    这不是「赛事」而是学习资源（category=学习资源，extra.is_learning_resource=True），
    供「baseline 精读」成稿变体整篇解读：原赛题任务、解题思路、关键代码。
    覆盖仓库内**全部**赛题目录——不限底层视觉，深度学习/AI 泛领域（NLP、表格、
    时序、推荐、CV……）均可入选；无截止时间概念，不参与 filter_active 时效过滤，
    由 fetch() 单独拼接，数量由 topics.yaml competitions.baseline.max_items 控制。
    """
    cfg = topics.raw.get("competitions", {}).get("baseline", {})
    if not cfg.get("enabled", True):
        return []
    data = await client.json(BASELINE_LIST_API, params={"per_page": 100},
                             headers=github_headers())
    if not isinstance(data, list):
        return []
    items: list[Item] = []
    now = datetime.now(timezone.utc)
    for row in data:
        if row.get("type") != "dir":
            continue
        name = strip_html(row.get("name", "") or "", 100)
        if not name:
            continue
        url = row.get("html_url") or f"https://github.com/datawhalechina/competition-baseline/tree/master/competition/{name}"
        item = Item(
            module="contest",
            title=f"往届 baseline：{name}",
            url=url,
            source="Datawhale baseline",
            published_at=now,
            summary="Datawhale 开源的往届竞赛 baseline 复现（含 notebook、解题思路与上分套路），适合赛前热身与学习同类赛题的通用打法。",
            category="学习资源",
            extra={
                "origin": "baseline",
                "platform": "GitHub",
                "prize": "—",
                "deadline": "",
                "entry_url": url,
                "is_learning_resource": True,
                "folder": name,  # 仓库里的目录名，fetch_baseline_detail 用
            },
        )
        item.score, item.keywords = score_item(item, topics)
        items.append(item)
    # 按分排序后限量：精读候选池不需要全部目录，留够深度即可（默认 30 份）
    items.sort(key=lambda x: x.score, reverse=True)
    return items[: int(cfg.get("max_items", 30))]


async def fetch_baseline_detail(client: HttpClient, folder: str) -> dict:
    """拉一个 baseline 目录的文件清单、README 摘录与关键代码片段，供「baseline 精读」成稿。

    返回 {"files", "readme_excerpt", "code_samples"?, "paradigm_hint"}；
    网络失败返回尽量少的 dict，不阻塞管线。

    兜底机制：README 为空或过短时不再让成稿裸奔——自动生成 paradigm_hint
    （赛事背景推断 + 任务形态推断 + 通用指标/CV/模型范式），交给成稿按
    「按此类赛事的通用范式」句式做专业推断，避免出现「未注明」这类暴露性表述。
    """
    folder_q = quote(folder)
    detail: dict = {}
    sizes: dict[str, int] = {}
    shas: dict[str, str] = {}

    async def _github_file(path: str) -> str:
        """经 api.github.com base64 内容接口取文件原文（raw.githubusercontent 在部分网络被挡）。"""
        data = await client.json(
            f"https://api.github.com/repos/datawhalechina/competition-baseline/"
            f"contents/competition/{path}",
            headers=github_headers(),
        )
        if data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8", "ignore")
        return ""

    async def _github_blob(sha: str) -> str:
        """走 Git Blob API 取文件原文。

        contents 接口对超过 1MB 的文件不返回 content（encoding=none），
        而 EDA notebook 动辄几 MB——blob 接口对任意大小都返回 base64。
        """
        data = await client.json(
            f"https://api.github.com/repos/datawhalechina/competition-baseline/git/blobs/{sha}",
            headers=github_headers(),
        )
        if data.get("encoding") == "base64" and data.get("content"):
            raw = str(data["content"]).replace("\n", "")
            return base64.b64decode(raw).decode("utf-8", "ignore")
        return ""

    try:
        entries = await client.json(
            f"https://api.github.com/repos/datawhalechina/competition-baseline/contents/competition/{folder_q}",
            params={"per_page": 100},
            headers=github_headers(),
        )
        # 不在此截断：选关键代码要在全量文件名里挑（payload 侧再做限量）
        detail["files"] = [e.get("name", "") for e in entries if e.get("type") == "file"]
        sizes = {e.get("name", ""): int(e.get("size") or 0) for e in entries if e.get("type") == "file"}
        shas = {e.get("name", ""): str(e.get("sha") or "") for e in entries if e.get("type") == "file"}
    except Exception:
        detail["files"] = []

    # README：主文件拿不到足够内容时依次试常见别名，都不行就靠范式推演兜底
    readme_text = ""
    for name in ("README.md", "readme.md", "README_CN.md", "README_EN.md"):
        if sizes and name not in sizes:
            continue  # 目录清单在手时直接跳过不存在的别名，省一次请求
        try:
            readme_text = await _github_file(f"{folder_q}/{quote(name)}")
        except Exception:
            continue
        if len(readme_text.strip()) >= 120:
            break
    # Markdown 里的图片/链接语法会干扰成稿，只留正文文本
    detail["readme_excerpt"] = strip_html(readme_text, 3500)

    # 关键代码：优先训练/主脚本，其次任意 .py，最后 notebook（抽取 code cell）
    def _code_priority(name: str) -> int:
        low = name.lower()
        for i, kw in enumerate(("train", "main", "run", "demo", "model")):
            if kw in low:
                return i
        return 99

    def _too_big(name: str) -> bool:
        size = sizes.get(name, 0)
        return size > _CODE_FILE_MAX_BYTES

    py_files = sorted(
        (f for f in detail["files"] if f.endswith(".py") and not _too_big(f)),
        key=_code_priority,
    )
    nb_files = [f for f in detail["files"] if f.endswith(".ipynb") and not _too_big(f)]
    picked = (py_files + nb_files)[:3]

    code_samples: list[dict] = []
    for name in picked:
        try:
            # 有 sha 优先走 blob 接口（>1MB 的文件 contents 接口不回内容）
            if shas.get(name):
                text = await _github_blob(shas[name])
            else:
                text = await _github_file(f"{folder_q}/{quote(name)}")
        except Exception:
            continue
        if name.endswith(".ipynb"):
            text = _ipynb_code(text)
        text = text.strip()
        if not text:
            continue
        code_samples.append({"name": name, "code": text[:2200]})
        await asyncio.sleep(0.4)
    if code_samples:
        detail["code_samples"] = code_samples

    # README 覆盖不了赛题信息时：先附范式推演提示，再联网检索官方背景做查证兜底。
    # search_context 让成稿以确定性语气直述赛题信息，避免出现「按目录名推断」这类推测话术。
    if len(detail["readme_excerpt"].strip()) < 120:
        detail["paradigm_hint"] = _paradigm_hint(folder, detail["files"])
        try:
            found = await search_competition_context(client, folder)
        except Exception:
            found = []
        if found:
            detail["search_context"] = found
    return detail


async def fetch(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {})
    groups = await asyncio.gather(
        fetch_kaggle(client), fetch_tianchi(client), fetch_aicrowd(client),
        fetch_web_search(client), fetch_curated(client),
        fetch_datacastle(client), fetch_coggle(client),
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
    upcoming_days = int(cfg.get("filter", {}).get("upcoming_days", 120))
    # 学习资源（往届 baseline）已注册为独立数据源「Datawhale baseline」，由 collect()
    # 并发抓取并做源级体检——不在此拼接，否则其失败会被静默吞掉
    return filter_active(list(merged.values()), upcoming_days=upcoming_days)
