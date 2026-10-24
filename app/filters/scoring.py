"""筛选与排序：先用规则把候选池收敛到 20~40 条，再交给大模型精排。

评分 = 关键词命中 + 会议/期刊等级 + 时效 + 可复现性（有代码/有图） - 噪声惩罚

为什么要规则 + 模型两级？
- 纯关键词召回高但精度低，直接喂给大模型会浪费 token 且容易跑题；
- 规则先砍掉 80% 噪声，模型只做「这 3 篇里哪篇最值得写」的判断，稳定又便宜。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Iterable

from ..config import Topics
from ..models import Item, normalize_title


def classify_category(text: str, topics: Topics) -> str:
    """按命中数量给素材打一个方向标签；全不命中时返回空串。"""
    lowered = (text or "").lower()
    best, best_hits = "", 0
    for label, words in topics.categories.items():
        hits = sum(1 for w in words if w.lower() in lowered)
        if hits > best_hits:
            best, best_hits = label, hits
    return best if best_hits else ""


def venue_bonus(venue: str, topics: Topics) -> float:
    if not venue:
        return 0.0
    best = 0.0
    upper = venue.upper()
    for name, weight in topics.venues.items():
        if name.upper() in upper:
            best = max(best, float(weight))
    return best


def freshness_bonus(published_at: datetime | None, half_life_days: int = 45) -> float:
    if not published_at:
        return 0.0
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age = max((now - published_at).total_seconds() / 86400, 0)
    # 指数衰减：0 天 +4，45 天 +2，90 天 +1
    return 4.0 * (0.5 ** (age / half_life_days))


def score_item(item: Item, topics: Topics) -> tuple[float, list[str]]:
    kw_score, hits = topics.keyword_weight(item.text_blob())
    score = kw_score
    score += venue_bonus(item.venue, topics) * 0.8
    score += freshness_bonus(item.published_at)
    score += 2.0 if item.code_url else 0.0
    score += 1.0 if item.image_url else 0.0
    score += 1.0 if item.authors else 0.0

    negative = [w for w in topics.raw.get("negative_keywords", []) if w in item.title]
    if negative:
        score -= 8.0 * len(negative)
        hits = [h for h in hits if h not in negative]
    return round(score, 2), hits


def is_fresh(item: Item, days: int) -> bool:
    if not item.published_at:
        return True
    published = item.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - published <= timedelta(days=days)


def _too_similar(title: str, others: Iterable[str], threshold: float = 0.86) -> bool:
    norm = normalize_title(title)
    for other in others:
        if SequenceMatcher(None, norm, normalize_title(other)).ratio() >= threshold:
            return True
    return False


def dedupe_history(
    items: list[Item],
    history_titles: Iterable[str],
    threshold: float = 0.85,
) -> tuple[list[Item], list[str]]:
    """把候选与「最近出稿」历史记录做标题相似度比对，挡掉高度相似的条目。

    fingerprint 去重只能拦「同一条素材」（同 URL）；同一篇论文被两个源收录、
    同一个赛事换了标题后缀、同一份 baseline 改了仓库名——这些 fingerprint
    不同但内容重复的候选，靠归一化标题的相似度兜底。防重的原则是宁可错杀：
    推一篇重复的损失远大于漏一篇候选。

    返回 (保留条目, 被挡条目的标题列表)；历史为空时原样返回。
    """
    history = [t for t in history_titles if t]
    if not history:
        return items, []
    kept: list[Item] = []
    dropped: list[str] = []
    for item in items:
        if _too_similar(item.title, history, threshold):
            dropped.append(item.title)
        else:
            kept.append(item)
    return kept, dropped


def filter_cfg(topics: Topics, module: str) -> dict:
    """各模块在 topics.yaml 里的筛选参数（select 与漏斗统计共用同一份口径）。"""
    if module == "paper":
        return topics.raw.get("academic", {}).get("filter", {})
    if module == "industry":
        return topics.raw.get("industry", {}).get("filter", {})
    return topics.raw.get("competitions", {}).get("filter", {})


def funnel_stats(
    items: Iterable[Item],
    topics: Topics,
    module: str,
    seen: set[str] | None = None,
) -> dict:
    """复算一次 select 的四层过滤漏斗，给出每层各挡掉多少条。

    select 结果为空时，运营需要知道是「分数不够」「关键词没命中」还是「全被去重」，
    而不是一个笼统的失败。口径与 select 完全一致（不含相似度/多样性层，那两层只在
    有通过者时才生效）。
    """
    cfg = filter_cfg(topics, module)
    min_score = float(cfg.get("min_score", 0.0))
    freshness_days = cfg.get("freshness_days", 45)
    require_keyword_hit = int(cfg.get("require_keyword_hit", 1))
    seen = seen or set()

    stats = {"total": 0, "seen": 0, "score": 0, "keywords": 0, "freshness": 0, "pass": 0}
    for item in items:
        stats["total"] += 1
        if item.fingerprint in seen:
            stats["seen"] += 1
        elif item.score < min_score:
            stats["score"] += 1
        elif len(item.keywords) < require_keyword_hit:
            stats["keywords"] += 1
        elif freshness_days and not is_fresh(item, freshness_days):
            stats["freshness"] += 1
        else:
            stats["pass"] += 1
    return stats


def select(
    items: Iterable[Item],
    topics: Topics,
    module: str,
    min_score: float | None = None,
    top_n: int | None = None,
    freshness_days: int | None = None,
    require_keyword_hit: int = 1,
    seen: set[str] | None = None,
    max_per_category: int = 2,
) -> list[Item]:
    """规则筛选 + 多样性挑选。"""
    cfg = topics.pipelines.get(module, {})
    default = filter_cfg(topics, module)

    min_score = default.get("min_score", 0.0) if min_score is None else min_score
    top_n = default.get("top_n", 3) if top_n is None else top_n
    freshness_days = default.get("freshness_days", 45) if freshness_days is None else freshness_days
    require_keyword_hit = int(default.get("require_keyword_hit", require_keyword_hit))
    seen = seen or set()

    pool: list[Item] = []
    kept_titles: list[str] = []
    for item in items:
        if item.fingerprint in seen:
            continue
        if item.score < min_score:
            continue
        if len(item.keywords) < require_keyword_hit:
            continue
        if freshness_days and not is_fresh(item, freshness_days):
            continue
        if _too_similar(item.title, kept_titles):
            continue
        kept_titles.append(item.title)
        pool.append(item)

    pool.sort(key=lambda x: (x.score, x.year_int), reverse=True)

    # 多样性：同一任务方向最多 max_per_category 条，避免一期全是超分
    picked: list[Item] = []
    counter: dict[str, int] = {}
    for item in pool:
        cat = item.category or "未分类"
        if counter.get(cat, 0) >= max_per_category:
            continue
        picked.append(item)
        counter[cat] = counter.get(cat, 0) + 1
        if len(picked) >= (top_n or 3):
            break
    return picked


def describe(item: Item) -> str:
    bits = [f"{item.score:.1f}分", item.category or "-", item.venue or "-"]
    if item.code_url:
        bits.append("有代码")
    return " | ".join(bits)
