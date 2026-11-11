"""赛事时效性闸门（Time-Validity Gatekeeper）：过期赛事一票否决。

为什么必须单独立一层？因为 `published_at` 是「这条素材被我们抓到的时间」，
**不是比赛的时间**。一个 2019 年的赛事页面今天被爬到，published_at 就是今天，
`scoring.is_fresh()` 这类按发布日期做的时效衰减对它完全失效——实测就是这样把
已截止六年的「FlyAI 超分挑战赛」放进了成稿流程，读者一眼看出这个号不更新。

判据按优先级排列，任何一条命中就否决：

1. **明确截止早于此刻** → 过期。不论数据源是否打了 `confirmed_active` / `evergreen`
   标记都否决：标记是源自己写的，日期是页面写的，页面更可信。
2. **没有任何未来日期，且全部日期证据都旧于「历史阈值」** → 历史老赛事（默认 2 年，
   即 2023 年及更早的场次在 2026 年一律不进候选）。
3. **既无日期线索、也无接口级「在办」标记** → 无法证明时效，否决。
   注意 `evergreen`（FlyAI 那类「长期有效练习赛」）**不算**在办证明：它正是
   2019 年赛事混进来的那条缝。

设计取向与防重一致：**宁可错杀**。漏一篇候选只是少一个选题，推一篇六年前的赛事
会直接毁掉账号可信度。

日期证据的来源分三档（从硬到软）：
- 结构化字段：`extra.deadline` / `extra.end` / `extra.start`；
- 官方页核验：`contest_detail.timeline` / `official_brief` / `page_excerpt` 里的完整日期；
- 标题年份：标题写「NTIRE 2024」这类，本身就是赛事届次声明。
摘要与正文只认**完整日期**（2026-11-18 / 2026年11月18日 / March 22, 2026），
不认裸年份——「自 2019 年起举办」这种叙述会把正常赛事误杀。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

# 比赛结束超过这个天数的一律视为历史老赛事（2 年：2026 年时 2023 年及更早出局）
HISTORY_DAYS = 730
# 没有任何未来日期时，日期证据的最大容许陈旧度：超了就是已结束的场次
STALE_DAYS = 120
# 推荐窗口：截止时间远于 N 天的赛事不写（读者点进去也报不了名）
UPCOMING_DAYS = 120

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

_FULL_DATE_RES = (
    # 2026-11-18 / 2026.11.18 / 2026/11/18（可带时刻）
    re.compile(r"(20\d{2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})"),
    # 2026年11月18日
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?"),
    # March 22, 2026 / Mar. 22 2026
    re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})", re.I),
    # 22 March 2026
    re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(20\d{2})", re.I),
)
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")

# 页面直接写「已结束 / 获奖名单公布」，是最省事的过期证据
_ENDED_WORDS = ("报名已结束", "报名已截止", "已截止报名", "已结束", "已完赛",
                "获奖名单", "圆满落幕", "registration closed", "has ended")

# 「截止 2019-11-18」「deadline: 2019-11-18」——正文里的截止日期（前缀词后紧跟日期）
_DEADLINE_TEXT_RE = re.compile(
    r"(?:报名截止|提交截止|作品提交截止|截止日?期?|deadline|due|closes?|ends?)"
    r"[^\d。；;\n]{0,12}"
    r"((?:20\d{2})[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2}\s*日?|"
    r"[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d{2})",
    re.I,
)


def _naive(dt: datetime | None) -> datetime | None:
    """统一成「本地 naive」：库里既有 naive 也有带时区的 ISO 串，混用会报 TypeError。"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt


def _mk(year: int, month: int, day: int, hh: int = 23, mi: int = 59) -> datetime | None:
    try:
        return datetime(year, month, day, hh, mi, 59)
    except ValueError:
        return None


def parse_date(text: str, *, hh: int = 23, mi: int = 59) -> datetime | None:
    """从单个字段/字符串里解析出**第一个**日期，失败返回 None。

    比 `competitions.parse_deadline` 宽松：不要求在开头、不剥前缀标签，
    因为调用方给的多半是整句（「报名截止：2026-11-18 23:59」）。
    """
    if not text:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    # 先试带时刻的 ISO：能保留时区与具体时刻，且对 "2026-11-18T12:00:00+08:00"
    # 与 "2026-11-18 23:59:59" 都最准。只有日期没有时刻的先跳过，交给下面的正则
    # 补成当天 23:59:59——否则「今天 23:59 截止」会在中午被判成已过期。
    for candidate in (raw, raw.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.hour or dt.minute or dt.second:
            return _naive(dt)
    for rx in _FULL_DATE_RES:
        m = rx.search(raw)
        if not m:
            continue
        a, b, c = m.group(1), m.group(2), m.group(3)
        if a.isdigit() and len(a) == 4:            # 年在前
            return _mk(int(a), int(b), int(c), hh, mi)
        # 英文月名两种语序
        month = _MONTHS.get((a if not a.isdigit() else b)[:3].lower())
        if month is None:
            return None
        day = int(b) if not a.isdigit() else int(a)
        return _mk(int(c), month, day, hh, mi)
    return None


def _text_dates(text: str) -> list[datetime]:
    """自由文本里的完整日期（不认裸年份，避免「自 2019 年起」误伤）。"""
    out: list[datetime] = []
    if not text:
        return out
    for rx in _FULL_DATE_RES:
        for m in rx.finditer(str(text)):
            a, b, c = m.group(1), m.group(2), m.group(3)
            if a.isdigit() and len(a) == 4:
                dt = _mk(int(a), int(b), int(c))
            else:
                month = _MONTHS.get((a if not a.isdigit() else b)[:3].lower())
                day = int(b) if not a.isdigit() else int(a)
                dt = _mk(int(c), month, day) if month else None
            if dt:
                out.append(dt)
    return out


def _title_years(title: str) -> list[datetime]:
    """标题里的年份按「该年最后一天」记：写 2026 就还有一整个 2026 可打。"""
    return [_mk(int(y), 12, 31) or datetime(int(y), 12, 31) for y in _YEAR_RE.findall(title or "")]


def _text_deadline(*texts: str) -> datetime | None:
    """正文里「截止 <日期>」写法的截止时间（取最早一条：报名口最先关）。"""
    found: list[datetime] = []
    for text in texts:
        for m in _DEADLINE_TEXT_RE.finditer(str(text or "")):
            dt = parse_date(m.group(1))
            if dt:
                found.append(dt)
    return min(found) if found else None


def _says_ended(*texts: str) -> bool:
    return any(w in str(text or "").lower() for text in texts for w in _ENDED_WORDS)


def _days(a: datetime, b: datetime) -> int:
    """按自然日算差：23:59:59 截止的「明天」就是剩 1 天，不是剩 0 天。"""
    return (a.date() - b.date()).days


def date_evidence(item: Any, detail: dict | None = None) -> list[tuple[datetime, str]]:
    """收集这条赛事的所有时间证据，返回 [(时间, 出处)]，按时间升序。"""
    extra = getattr(item, "extra", {}) or {}
    if detail is None:
        detail = extra.get("contest_detail") or {}
    found: list[tuple[datetime, str]] = []

    for key, label in (("deadline", "截止字段"), ("end", "结束字段"), ("start", "开赛字段")):
        raw = str(extra.get(key) or "")
        dt = parse_date(raw)
        if dt:
            found.append((dt, f"{label}：{raw[:40]}"))

    for raw in (detail.get("timeline") or [])[:8]:
        dt = parse_date(str(raw))
        if dt:
            found.append((dt, f"官方页时间线：{str(raw)[:40]}"))

    for text, label in ((detail.get("official_brief"), "官方赛题正文"),
                        (detail.get("page_excerpt"), "官方页面"),
                        (getattr(item, "summary", ""), "摘要")):
        for dt in _text_dates(str(text or ""))[:12]:
            found.append((dt, f"{label}：{dt:%Y-%m-%d}"))

    for dt in _title_years(getattr(item, "title", "")):
        found.append((dt, f"标题年份：{dt.year}"))

    return sorted(found, key=lambda x: x[0])


@dataclass
class Verdict:
    """一条赛事的时效判定结果。"""

    status: str                      # active | expired | historical | windowed | undated | learning
    reason: str = ""
    deadline: datetime | None = None
    latest: datetime | None = None
    days_left: int | None = None
    verified: bool = True            # False = 没有日期，靠源接口的「在办」标记放行

    @property
    def ok(self) -> bool:
        return self.status in ("active", "learning")


def _cfg(topics_obj: Any | None = None) -> dict:
    if topics_obj is not None:
        return (getattr(topics_obj, "raw", {}) or {}).get("competitions", {}).get("filter", {}) or {}
    try:
        from ..config import topics as _topics

        return (_topics.raw or {}).get("competitions", {}).get("filter", {}) or {}
    except Exception:      # 配置没加载成功也不该让「过期拦截」失效，退回默认阈值
        return {}


def evaluate(item: Any, now: datetime | None = None, *,
             detail: dict | None = None, upcoming_days: int | None = None,
             history_days: int | None = None, stale_days: int | None = None,
             topics_obj: Any | None = None) -> Verdict:
    """对单条赛事做时效判定。纯函数，不修改 item。"""
    extra = getattr(item, "extra", {}) or {}
    if extra.get("is_learning_resource"):
        # 往届 baseline / NTIRE 报告是「学习资料」，没有截止时间概念，不参与时效判定
        return Verdict(status="learning", reason="学习资源（无赛期概念）")

    cfg = _cfg(topics_obj)
    upcoming_days = int(upcoming_days if upcoming_days is not None
                        else cfg.get("upcoming_days", UPCOMING_DAYS))
    history_days = int(history_days if history_days is not None
                       else cfg.get("history_days", HISTORY_DAYS))
    stale_days = int(stale_days if stale_days is not None else cfg.get("stale_days", STALE_DAYS))
    now = now or datetime.now()

    if detail is None:
        detail = extra.get("contest_detail") or {}

    evidence = date_evidence(item, detail)
    dates = [d for d, _ in evidence]
    deadline = parse_date(str(extra.get("deadline") or "")) or parse_date(str(extra.get("end") or ""))
    page_text = " ".join(str(detail.get(k) or "") for k in
                         ("official_brief", "page_excerpt", "registration_hint"))
    shown_deadline = _text_deadline(page_text, getattr(item, "summary", ""))
    latest = dates[-1] if dates else None
    future = [d for d in dates if d > now]

    # ① 明确截止已过 → 一票否决（不因 confirmed_active / evergreen 放行）
    deadline = deadline or shown_deadline
    if deadline is not None and deadline <= now:
        past = -_days(deadline, now)
        return Verdict("expired",
                       f"截止 {deadline:%Y-%m-%d} 已过 {past} 天",
                       deadline=deadline, latest=latest)
    # 页面直接写「报名已结束 / 获奖名单公布」，且没有未来日期 → 过期
    if _says_ended(page_text) and not future:
        return Verdict("expired", "官方页面标注已结束/已截止，且无未来节点",
                       deadline=deadline, latest=latest)
    if deadline is not None and _days(deadline, now) > upcoming_days:
        return Verdict("windowed",
                       f"截止 {deadline:%Y-%m-%d} 距今 {_days(deadline, now)} 天，"
                       f"超出 {upcoming_days} 天推荐窗口",
                       deadline=deadline, latest=latest)
    if deadline is not None:
        days_left = max(_days(deadline, now), 0)
        return Verdict("active", f"截止 {deadline:%Y-%m-%d}，剩 {days_left} 天",
                       deadline=deadline, latest=latest, days_left=days_left)

    # ② 没有截止字段，但页面/正文里有未来日期（如报名截止写在正文里）→ 算在办
    if future:
        soonest = min(future)
        if _days(soonest, now) > upcoming_days * 2:
            return Verdict("windowed",
                           f"最近的日期 {soonest:%Y-%m-%d} 距今天 {_days(soonest, now)} 天",
                           latest=latest)
        return Verdict("active", f"页面时间线最近节点 {soonest:%Y-%m-%d}",
                       latest=latest, days_left=max(_days(soonest, now), 0))

    # ③ 只有过去日期：超过陈旧阈值就是已经结束的场次
    if latest is not None:
        age = -_days(latest, now)
        if age > stale_days:
            status = "historical" if age > history_days else "expired"
            hint = "历史老赛事" if status == "historical" else "赛程已结束"
            return Verdict(status, f"最后一条时间证据 {latest:%Y-%m-%d} 距今 {age} 天（{hint}）",
                           latest=latest)

    # ④ 完全没有日期线索：只有接口级「在办」标记能放行；evergreen 不算证据
    if extra.get("confirmed_active") and not extra.get("evergreen"):
        return Verdict("active", "无日期线索，按数据源「在办」标记放行", latest=latest,
                       verified=False)
    if extra.get("evergreen"):
        return Verdict("undated",
                       "标称「长期开放」但无任何日期可核验：2019 年这类老场次正是从这里混进来的",
                       latest=latest)
    return Verdict("undated", "无截止时间、也无可核验日期", latest=latest)


def gate(items: Iterable[Any], now: datetime | None = None, *,
         upcoming_days: int | None = None, topics_obj: Any | None = None
         ) -> tuple[list[Any], list[dict]]:
    """批量时效闸门：返回 (保留条目, 否决明细)。保留者会补 `days_left` 与分数。

    否决明细是给日志与运营看的（标题 + 原因），不静默丢弃——「为什么这期没稿」
    必须能在控制台里读出来。
    """
    now = now or datetime.now()
    cfg = _cfg(topics_obj)
    upcoming_days = int(upcoming_days if upcoming_days is not None
                        else cfg.get("upcoming_days", UPCOMING_DAYS))

    kept: list[Any] = []
    rejected: list[dict] = []
    for item in items:
        v = evaluate(item, now, upcoming_days=upcoming_days, topics_obj=topics_obj)
        if not v.ok:
            rejected.append({"title": getattr(item, "title", ""), "status": v.status,
                             "reason": v.reason, "source": getattr(item, "source", "")})
            continue
        item.extra["recency"] = {
            "status": v.status,
            "deadline": v.deadline.isoformat() if v.deadline else "",
            "verified": v.verified,
        }
        # 与旧 filter_active 的加分成一致：有明确未来截止 +2，快截止再 +1，
        # 靠接口在办标记放行的 +1（没日期但确实是新场次）
        if v.deadline is not None:
            item.score += 2.0
            if v.days_left is not None and v.days_left <= 30:
                item.score += 1.0
            item.extra["days_left"] = v.days_left
        elif v.status == "active" and not v.verified:
            item.score += 1.0
            item.extra.pop("days_left", None)
        kept.append(item)
    return kept, rejected


def describe_rejects(rejected: list[dict], limit: int = 3) -> str:
    """否决明细压成一行，给日志用。"""
    if not rejected:
        return ""
    head = "；".join(f"{r['title'][:22]}（{r['reason'][:36]}）" for r in rejected[:limit])
    more = f" 等 {len(rejected)} 条" if len(rejected) > limit else ""
    return head + more


def _iter_spec_texts(spec: dict):
    """把 spec 里所有可能承载日期的文本摊平（导语 / 正文 / 信息卡 / 表格）。"""
    for key in ("title", "subtitle", "digest", "takeaway"):
        if isinstance(spec.get(key), str):
            yield spec[key]
    for para in spec.get("lead") or []:
        if isinstance(para, str):
            yield para
    for sec in spec.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        if isinstance(sec.get("text"), str):
            yield sec["text"]
        for row in sec.get("rows") or []:
            if isinstance(row, (list, tuple)):
                yield "：".join(str(c) for c in row)
            elif isinstance(row, dict):
                yield "：".join(str(v) for v in row.values())
            elif isinstance(row, str):
                yield row


def stale_claims(spec: dict, now: datetime | None = None) -> list[str]:
    """成稿自检：正文里把「已过期的日期」当成交付节点写出来的句子。

    闸门已保证素材本身不过期，这一层防的是日期写错年份（把 2026 写成 2024）。
    只报不删——交给运营复核，误删正文的代价比留一句提示大得多。
    """
    now = now or datetime.now()
    hits: list[str] = []
    for text in _iter_spec_texts(spec):
        if not any(w in text for w in ("截止", "报名", "提交", "结束")):
            continue
        for dt in _text_dates(text):
            if (now - dt).days > 30:
                hits.append(f"{dt:%Y-%m-%d}「{text[:40]}」")
                break
    return hits
