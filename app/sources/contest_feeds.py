"""模块 C 的新增赛事雷达源：AI 赛事雷达 / ML Contests / Codabench / FlyAI。

四个源的取数方式各不相同，都是实测出来的：

1. **AI 赛事雷达**（ye4wzp.github.io/ai-contest-radar）：纯静态站，首页只有骨架，
   数据在 `data/data.js` 里以 `window.__DATA__ = {...}` 形式内嵌。831 条赛事，
   **`detail` 字段直接带「一、赛事背景 / 二、赛事任务 / 三、评审规则」的成段文字**——
   这是单篇精读最缺的素材，等于别人替你做完了官方页面的正文抽取。
2. **ML Contests**（mlcontests.com）：Astro 静态页，赛事数据塞在
   `<div id="competitions" data-competitions="[{...}]">` 这个属性里（HTML 转义过的 JSON），
   解一次 `html.unescape` 就能 `json.loads`。
3. **Codabench**（codabench.org）：直连会掉进 iocaine 蜜罐（返回《1984》随机段落，
   状态码 200）。用无头浏览器打开 DRF 的可浏览 API 页，从 `pre.prettyprint` 里
   把 JSON 抠出来。列表**不含截止时间**，只在 `first_phase_start` 有开赛日，
   因此标 `needs_verify`，交给成稿前的官方页核验。
4. **FlyAI**（static.flyai.com）：主站有瑞数验证码（无头也拦），静态镜像站可直抓。
   它的赛题多为「长期有效」的练习赛，标 `evergreen`，不参与截止倒计时。

所有条目统一成 Item，结构化字段放 extra（platform / prize / deadline / entry_url /
contest_detail），由 `filter_active` 做时效过滤、`fetch_contest_detail` 做二次核验。
"""
from __future__ import annotations

import html as html_mod
import json
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from ..config import topics
from ..filters.scoring import score_item
from ..models import Item
from . import headless
from .base import HttpClient, strip_html
from .competitions import _contest_category, parse_deadline

RADAR_DATA = "https://ye4wzp.github.io/ai-contest-radar/data/data.js"
MLCONTESTS_URL = "https://mlcontests.com/"
CODABENCH_API = "https://www.codabench.org/api/competitions/?page=1&page_size=40"
FLYAI_URL = "https://static.flyai.com/"

_PRIZE_RE = re.compile(r"(?:奖金池|专属奖金|总奖池|总奖金)[^0-9¥￥$]{0,6}(?:[¥￥$]\s*[\d,.]+\s*[万亿]?|[\d,.]+\s*万元?)")

# 公开竞赛广场有垃圾投稿（赌场/博彩/成人），标题命中即丢
_SPAM_WORDS = ("casino", "slot", "betting", "payid", "porn", "escort", "viagra",
               "crypto giveaway", "赌博", "博彩", "娱乐城", "时时彩", "彩票代")


def _is_spam(title: str) -> bool:
    low = (title or "").lower()
    return any(w in low for w in _SPAM_WORDS)


# ------------------------------------------------------------------ 公共
def _first_date(*values: str) -> tuple[str, datetime | None]:
    """按优先级取第一个能解析的日期串，返回 (原始串, 解析结果)。"""
    for raw in values:
        if not raw:
            continue
        dt = parse_deadline(str(raw))
        if dt:
            return str(raw), dt
    return "", None


def _detail_brief(text: str, limit: int = 3600) -> str:
    """官方赛题正文（雷达的 detail 是 markdown，压掉多余空行后直接给模型）。"""
    if not text:
        return ""
    cleaned = re.sub(r"\n{3,}", "\n\n", str(text)).strip()
    return cleaned[:limit]


def _finish(item: Item, *, bonus: float = 0.0) -> Item:
    item.score, item.keywords = score_item(item, topics)
    item.score += bonus
    return item


# ------------------------------------------------------------------ AI 赛事雷达
def parse_radar(text: str) -> list[Item]:
    """解析 data.js：`window.__DATA__ = {...};`。"""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    rows = payload.get("competitions") or []
    now = datetime.now()
    items: list[Item] = []
    for row in rows:
        title = strip_html(str(row.get("name") or ""), 160)
        if not title or _is_spam(title):
            continue
        signup = str(row.get("signup") or "").lower()
        if signup == "closed":
            continue  # 报名已关，铁律：不进候选
        deadline_raw, deadline = _first_date(row.get("deadline") or "", row.get("end") or "")
        start_raw, start = _first_date(row.get("start") or "")
        # 报名截止、比赛结束都没有，但状态是 open/upcoming：按「确认进行中」放行
        confirmed = bool(deadline is None and signup in ("open", "upcoming"))
        if deadline is None and not confirmed:
            continue
        if deadline and deadline <= now:
            continue
        desc = strip_html(str(row.get("description") or ""), 600)
        tags = [str(t) for t in (row.get("tags") or []) if t][:8]
        prize = strip_html(str(row.get("prize") or ""), 60)
        brief = _detail_brief(row.get("detail") or "")
        url = str(row.get("official_url") or "").strip()
        if not url:
            srcs = row.get("sources") or []
            url = str((srcs[0] or {}).get("url") or "") if srcs else ""
        item = Item(
            module="contest",
            title=title,
            url=url,
            source="AI 赛事雷达",
            published_at=datetime.now(timezone.utc),
            summary=desc,
            category=_contest_category(f"{title} {desc} {' '.join(tags)}"),
            extra={
                "origin": "radar",
                "platform": strip_html(str(row.get("organizer") or ""), 60) or "AI 赛事雷达",
                "organizer": strip_html(str(row.get("organizer") or ""), 60),
                "track": strip_html(str(row.get("type") or ""), 40),
                "prize": prize,
                "deadline": deadline_raw or (deadline.isoformat() if deadline else ""),
                "start": start_raw,
                "end": str(row.get("end") or ""),
                "signup": signup,
                "entry_url": url,
                "tags": tags,
                "confirmed_active": confirmed,
                "contest_detail": {
                    "official_brief": brief,
                    "source": "AI 赛事雷达（官方赛题页摘要）",
                },
            },
        )
        bonus = 1.5 if row.get("featured") else 0.0
        if prize:
            bonus += 0.5
        if brief:
            bonus += 1.2  # 带「背景/任务/评审规则」正文的，精读能落到细节
        if item.category in topics.categories:
            bonus += 0.8  # 同分时偏向底层视觉/CV，符合账号定位
        items.append(_finish(item, bonus=bonus))
    return items


async def fetch_radar(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("radar", {})
    if not cfg.get("enabled", True):
        return []
    text = await client.text(cfg.get("data_url", RADAR_DATA))
    items = parse_radar(text)
    print(f"[contest] AI 赛事雷达：{len(items)} 条在办/待开始", flush=True)
    return items


# ------------------------------------------------------------------ ML Contests
def parse_mlcontests(html: str) -> list[Item]:
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one("[data-competitions]")
    if not node:
        return []
    raw = html_mod.unescape(node.get("data-competitions") or "")
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return []
    now = datetime.now()
    items: list[Item] = []
    for row in rows if isinstance(rows, list) else []:
        title = strip_html(str(row.get("name") or ""), 160)
        if not title or _is_spam(title):
            continue
        deadline_raw, deadline = _first_date(
            row.get("registration-deadline") or "", row.get("deadline") or "")
        if deadline and deadline <= now:
            continue
        if deadline is None:
            continue  # 没有截止线索的一律丢弃（时效铁律）
        tags = [str(t) for t in (row.get("tags") or []) if t][:8]
        platform = strip_html(str(row.get("platform") or ""), 40) or "ML Contests"
        sponsor = strip_html(str(row.get("sponsor") or ""), 60)
        desc_bits = [b for b in (sponsor, str(row.get("conference") or "")) if b]
        item = Item(
            module="contest",
            title=title,
            url=str(row.get("url") or ""),
            source="ML Contests",
            published_at=datetime.now(timezone.utc),
            summary=strip_html(str(row.get("description") or " · ".join(desc_bits)), 600),
            category=_contest_category(f"{title} {' '.join(tags)} {desc_bits}"),
            extra={
                "origin": "mlcontests",
                "platform": platform,
                "organizer": sponsor,
                "prize": strip_html(str(row.get("prize") or ""), 60),
                "deadline": deadline_raw,
                "start": str(row.get("launched") or ""),
                "entry_url": str(row.get("url") or ""),
                "tags": tags,
                "conference": strip_html(str(row.get("conference") or ""), 60),
                "contest_detail": {"source": "ML Contests"},
            },
        )
        items.append(_finish(item, bonus=1.0 if row.get("prize") else 0.0))
    return items


async def fetch_mlcontests(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("mlcontests", {})
    if not cfg.get("enabled", True):
        return []
    html = await client.text(cfg.get("url", MLCONTESTS_URL))
    items = parse_mlcontests(html)
    print(f"[contest] ML Contests：{len(items)} 条未截止", flush=True)
    return items


# ------------------------------------------------------------------ Codabench
def extract_drf_json(html: str) -> dict | list | None:
    """从 DRF 可浏览 API 页里抠出响应 JSON。

    页面结构是 `pre.prettyprint` × N：第一个是请求行，第二个是
    `HTTP 200 OK ...` 响应头 + JSON 正文，所以要从第一个 `{` 开始截。
    """
    soup = BeautifulSoup(html, "lxml")
    for pre in soup.select("pre.prettyprint"):
        text = pre.get_text()
        idx = text.find("{")
        if idx < 0:
            continue
        try:
            return json.loads(text[idx:])
        except json.JSONDecodeError:
            continue
    return None


def parse_codabench(payload: dict | list | None) -> list[Item]:
    rows = payload.get("results") if isinstance(payload, dict) else payload
    now = datetime.now()
    items: list[Item] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = strip_html(str(row.get("title") or ""), 160)
        if not title or _is_spam(title):
            continue
        created = parse_deadline(str(row.get("created_when") or ""))
        # 只保留近期上架 / 有奖金的：1531 条里大半是历史赛事，不筛会淹掉候选池
        if created and (now - created) > timedelta(days=180):
            continue
        start_raw = str(row.get("first_phase_start") or "")
        start = parse_deadline(start_raw) if start_raw else None
        if start and (now - start) > timedelta(days=730):
            continue  # 开赛两年以上的老场次，多半早已结束，不给核验添负担
        desc = strip_html(str(row.get("description") or ""), 600)
        reward = strip_html(str(row.get("reward") or ""), 40)
        url = f"https://www.codabench.org/competitions/{row.get('id')}"
        item = Item(
            module="contest",
            title=title,
            url=url,
            source="Codabench",
            published_at=created or datetime.now(timezone.utc),
            summary=desc,
            category=_contest_category(f"{title} {desc}"),
            extra={
                "origin": "codabench",
                "platform": "Codabench",
                "organizer": strip_html(str(row.get("owner_display_name") or ""), 60),
                "prize": f"${reward}" if reward and not reward.startswith("$") else reward,
                "deadline": "",  # 列表接口不给截止时间，成稿前用官方页核验
                "start": start_raw,
                "entry_url": url,
                "participants": row.get("participants_count"),
                # 公开列表里的场次默认在办；具体截止时间由 needs_verify 二次核验
                "confirmed_active": True,
                "needs_verify": True,
                "contest_detail": {"source": "Codabench 公开竞赛页"},
            },
        )
        items.append(_finish(item, bonus=0.8 if row.get("is_featured") else 0.0))
    return items


async def fetch_codabench(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("codabench", {})
    if not cfg.get("enabled", True):
        return []
    url = cfg.get("api_url", CODABENCH_API)
    # 先试直连：多数环境会被蜜罐挡下，能直连就省一次浏览器启动
    try:
        body = await client.text(url)
    except Exception:
        body = ""
    if headless.looks_like_honeypot(body):
        body = await headless.render_dom(url, timeout=60.0, virtual_ms=15000)
    items = parse_codabench(extract_drf_json(body))
    print(f"[contest] Codabench：{len(items)} 条近期公开赛事", flush=True)
    return items


# ------------------------------------------------------------------ FlyAI
def parse_flyai(html: str) -> list[Item]:
    soup = BeautifulSoup(html, "lxml")
    items: list[Item] = []
    for card in soup.select("div.compatibleIE"):
        h2 = card.select_one("h2")
        if not h2:
            continue
        title = strip_html(h2.get_text(" ", strip=True), 160)
        status = strip_html((card.select_one("p.noReg") or card.select_one(".status")).get_text(" ", strip=True), 20) if (card.select_one("p.noReg") or card.select_one(".status")) else ""
        if status and "报名" not in status:
            continue
        link = card.find_parent("a", href=True)
        url = link.get("href") if link else ""
        bonus_node = card.select_one(".bonus")
        bonus_txt = strip_html(bonus_node.get_text(" ", strip=True), 40) if bonus_node else ""
        prize = ""
        m = _PRIZE_RE.search(bonus_txt)
        if m:
            prize = m.group(0)
        elif bonus_txt:
            prize = bonus_txt
        desc = strip_html((card.select_one(".describe") or card).get_text(" ", strip=True), 500)
        tags = [a.get_text(" ", strip=True) for a in card.select(".pro_tip a.level")][:6]
        item = Item(
            module="contest",
            title=title,
            url=url,
            source="FlyAI",
            published_at=datetime.now(timezone.utc),
            summary=desc,
            category=_contest_category(f"{title} {desc} {' '.join(tags)}"),
            extra={
                "origin": "flyai",
                "platform": "FlyAI",
                "prize": prize,
                # 平台赛题以「长期有效」的练习赛为主，没有报名截止；标 evergreen，
                # 时效过滤按「长期开放」放行，但不参与倒计时加分
                "deadline": "",
                "entry_url": url,
                "tags": tags,
                "evergreen": True,
                "confirmed_active": True,
                "contest_detail": {"source": "FlyAI 赛事页"},
            },
        )
        items.append(_finish(item, bonus=-3.0))
    return items


async def fetch_flyai(client: HttpClient) -> list[Item]:
    cfg = topics.raw.get("competitions", {}).get("flyai", {})
    if not cfg.get("enabled", True):
        return []
    body = await client.text(cfg.get("url", FLYAI_URL))
    items = parse_flyai(body)
    print(f"[contest] FlyAI：{len(items)} 条长期开放赛题", flush=True)
    return items
