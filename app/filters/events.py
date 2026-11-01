"""业界进展的事件级查重：同一事件的多角度报道，不许换汤不换药地重复出稿。

为什么标题相似度不够：实测同一天出过两篇——
「OpenAI 融资目标砍掉三分之一：300 亿变 200 亿」与
「OpenAI 融资目标砍掉 100 亿美元，估值降至 1.25 万亿」。
两条标题字面相似度只有 0.5 上下（SequenceMatcher 拦不住），但它们是同一个事件的
两篇报道：主体相同（OpenAI）、事件类型相同（融资）、数字只是换了个说法
（100 亿 = 300 亿 - 200 亿）。这类重复必须按「事件」判，而不是按「标题字面」。

事件签名 = 核心实体（谁）+ 事件类型（干了什么）+ 关键数字（多少钱/多少分）。
判重仍然是「宁可错杀」：重复推一篇的损失，远大于漏掉一条候选。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..models import Item

# ---------------------------------------------------------------- 实体词表
# 只收「会在标题里当主语」的名字。宁可少收，也不要把「智能 / 科技 / 芯片」
# 这类通用词收进来——它们会让两篇无关稿件被误判成同一个事件。
_CJK_BRANDS: dict[str, str] = {
    "阿里巴巴": "阿里", "阿里云": "阿里", "阿里": "阿里",
    "腾讯云": "腾讯", "腾讯": "腾讯",
    "百度智能云": "百度", "百度": "百度",
    "字节跳动": "字节", "字节": "字节", "抖音": "字节",
    "华为云": "华为", "华为": "华为",
    "小米": "小米", "荣耀": "荣耀", "中兴": "中兴", "联想": "联想",
    "商汤": "商汤", "旷视": "旷视", "依图": "依图", "云从": "云从",
    "智谱": "智谱", "智谱ai": "智谱", "月之暗面": "月之暗面", "百川智能": "百川",
    "阶跃星辰": "阶跃星辰", "零一万物": "零一万物", "深度求索": "deepseek",
    "面壁智能": "面壁", "出门问问": "出门问问", "科大讯飞": "科大讯飞",
    "寒武纪": "寒武纪", "摩尔线程": "摩尔线程", "壁仞科技": "壁仞",
    "燧原科技": "燧原", "天数智芯": "天数智芯", "地平线": "地平线",
    "黑芝麻智能": "黑芝麻", "小马智行": "小马智行", "文远知行": "文远知行",
    "禾赛科技": "禾赛", "速腾聚创": "速腾聚创",
    "宇树科技": "宇树", "智元机器人": "智元", "银河通用": "银河通用",
    "星动纪元": "星动纪元", "逐际动力": "逐际动力", "傅利叶": "傅利叶",
    "优必选": "优必选", "大疆": "大疆",
    "比亚迪": "比亚迪", "蔚来": "蔚来", "小鹏汽车": "小鹏", "小鹏": "小鹏",
    "理想汽车": "理想汽车", "吉利": "吉利", "长安汽车": "长安",
    "广汽": "广汽", "上汽": "上汽", "一汽": "一汽", "长城汽车": "长城",
    "赛力斯": "赛力斯", "康佳": "康佳", "海信": "海信", "创维": "创维",
    "浪潮信息": "浪潮", "新华三": "新华三",
    "西湖大学": "西湖", "新芯航途": "新芯航途",
    "中芯国际": "中芯", "华虹": "华虹", "长江存储": "长江存储", "长鑫存储": "长鑫",
}

_LATIN_BRANDS: dict[str, str] = {
    "openai": "openai", "open ai": "openai", "anthropic": "anthropic",
    "google": "google", "deepmind": "deepmind", "google deepmind": "deepmind",
    "microsoft": "microsoft", "nvidia": "nvidia", "amd": "amd", "intel": "intel",
    "tsmc": "tsmc", "apple": "apple", "amazon": "amazon", "aws": "aws",
    "tesla": "tesla", "xai": "xai", "mistral": "mistral", "cohere": "cohere",
    "runway": "runway", "figure": "figure", "boston dynamics": "boston dynamics",
    "hugging face": "huggingface", "huggingface": "huggingface",
    "perplexity": "perplexity", "databricks": "databricks", "stability ai": "stability",
    "samsung": "samsung", "qualcomm": "qualcomm", "arm": "arm", "sk hynix": "skhynix",
    "micron": "micron", "asml": "asml", "broadcom": "broadcom",
    "tiktok": "字节", "bytedance": "字节", "deepseek": "deepseek",
    "minimax": "minimax", "通义千问": "阿里",
    "momenta": "momenta", "waymo": "waymo",
    "cruise": "cruise", "meta": "meta", "oracle": "oracle", "ibm": "ibm",
}

# 这些词在英文里是常用词，小写出现时不算实体（「meta analysis」「arm 架构」）
_CASE_SENSITIVE_BRANDS = {"meta", "arm", "figure", "apple", "runway", "scale ai"}

_LATIN_STOP: frozenset[str] = frozenset("""
ai agi ml dl cv nlp llm llms gpu gpus npu tpu cpu api apis sdk sdk cpu soc
ceo cto coo cfo ipo usd rmb cny eur jpy pdf html json http https url uri
usa eu uk china chinese english january february march april may june july august
september october november december monday tuesday wednesday thursday friday
the a an this that these those new next last first second third
we our you your they their it its he she his her
and or but if then than as at by for from in into of on to with without
is are was were be been being has have had do does did can could may might
will would should must not no yes all any some more most other another
today yesterday tomorrow week month year day time report news update
ai news tech techcrunch bloomberg reuters ft wsj verge wired fortune forbes
large pro max mini ultra plus lite turbo flash nano base small medium
open image denoise super resolution video audio model models agent agents robot robotics
world vision language deep neural network networks learning dataset datasets benchmark
benchmarks diffusion transformer prompt prompts reasoning edge cloud data compute chip
chips hardware software platform systems tool tools framework engine studio lab labs
research institute university tech technology digital smart next future first second
best top source weights license paper papers arxiv github
""".split())

# 中文机构名的后缀兜底。捕获组拿到的是「后缀前的 2–8 个汉字」，
# 必须再过一遍黑名单，否则「一家人工智能公司」会被当成实体「一家人工智能」。
_CJK_ORG_RE = re.compile(
    r"([\u4e00-\u9fff]{2,8})(?:公司|集团|科技|数智|大学|研究院|实验室|控股|汽车|机器人)")
_CJK_ORG_STOP: frozenset[str] = frozenset("""
一家 这家 那家 该 多家 初创 上述 本 子 母 合资 上市 人工 智能 科技 芯片 算力
模型 大模型 数据 平台 系统 算法 开源 中国 美国 全球 国内 海外 行业 产业 头部
传统 新兴 相关 核心 整个 部分 多数 少数 这家 一家 两家 三家 若干 多家 各家
""".split())

# 品牌规范名集合：判断「有没有共同主体」时，品牌比泛专名可靠得多。
# 两个 WebGPU 项目共享「webgpu」这种技术词，不算同一件事；共享 OpenAI 才算。
_BRAND_CANONICAL = frozenset(_CJK_BRANDS.values()) | frozenset(_LATIN_BRANDS.values())

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

def _latin_matcher(words: Iterable[str], ignore_case: bool) -> re.Pattern:
    alts = sorted({re.escape(w) for w in words}, key=len, reverse=True)
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])", flags)


_CI_LATIN = _latin_matcher(
    [w for w in _LATIN_BRANDS if w not in _CASE_SENSITIVE_BRANDS and " " not in w], True)
_CI_LATIN_PHRASE = _latin_matcher(
    [w for w in _LATIN_BRANDS if w not in _CASE_SENSITIVE_BRANDS and " " in w], True)
_CS_LATIN = _latin_matcher([w for w in _CASE_SENSITIVE_BRANDS], False)
# 拉丁专名：首字母大写的词（OpenAI / Momenta / SparkVSR），全大写缩写不算——
# 缩写（GPU/TOPS/SOTA）是术语，不是主体。
_PROPER_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9]*(?:[-−][A-Za-z0-9]+)*")


def _is_junk_entity(token: str) -> bool:
    low = token.lower()
    if not low or low in _LATIN_STOP:
        return True
    if len(token) <= 2:
        return True
    if token in _CJK_ORG_STOP:
        return True
    return any(token.startswith(bad) for bad in ("一家", "这家", "那家", "多家", "该", "本"))


def entities(text: str) -> frozenset[str]:
    """抽「谁」：品牌词表 + 拉丁专名 + 中文机构后缀，统一成规范名。"""
    blob = str(text or "")
    found: set[str] = set()
    low = blob.lower()

    for variant, canonical in _CJK_BRANDS.items():
        if variant in low:
            found.add(canonical)
    for m in _CI_LATIN.finditer(blob):
        canonical = _LATIN_BRANDS.get(m.group(0).lower())
        if canonical:
            found.add(canonical)
    for m in _CI_LATIN_PHRASE.finditer(blob):
        canonical = _LATIN_BRANDS.get(m.group(0).lower())
        if canonical:
            found.add(canonical)
    for m in _CS_LATIN.finditer(blob):
        canonical = _LATIN_BRANDS.get(m.group(0).lower())
        if canonical:
            found.add(canonical)
    for m in _PROPER_RE.finditer(blob):
        token = m.group(0)
        if token.isupper() and token.lower() not in _LATIN_BRANDS:
            continue
        if not _is_junk_entity(token):
            found.add(token.lower())
    for m in _CJK_ORG_RE.finditer(blob):
        token = m.group(1)
        if _is_junk_entity(token):
            continue
        found.add(token)
    return frozenset(found)


# ---------------------------------------------------------------- 事件类型
# 顺序即优先级：融资排在最前，因为「同一轮融资被多家媒体换数字报道」是最常见的重复源。
_EVENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("融资", ("融资", "募资", "估值", "轮融资", "种子轮", "天使轮", "funding", "valuation", "raise")),
    ("并购", ("收购", "并购", "合并", "acquire", "acquisition", "merger")),
    ("上市退市", ("ipo", "上市", "招股", "挂牌", "退市", "delist")),
    ("监管诉讼", ("诉讼", "起诉", "监管", "调查", "罚款", "反垄断", "禁令", "lawsuit", "antitrust")),
    ("财报业绩", ("财报", "营收", "净利润", "亏损", "业绩", "revenue", "earnings", "profit")),
    ("人事变动", ("离职", "辞职", "出任", "任命", "创始人", "resign", "appoint", "hire")),
    ("芯片算力", ("芯片", "soc", "gpu", "npu", "算力", "制程", "晶圆", "流片", "tops", "半导体", "chip")),
    ("模型发布", ("发布", "推出", "上线", "开源", "开放权重", "release", "launch", "open-source")),
    ("战略合作", ("合作", "共建", "签约", "联手", "战略", "partner", "collaborat")),
    ("技术研究", ("论文", "研究", "实验", "突破", "paper", "research")),
)


def event_type(text: str) -> str:
    """抽「干了什么」。命中数最多的类胜出，同分按上表顺序（越靠前越具体）。"""
    low = str(text or "").lower()
    best, best_hits = "其他", 0
    for label, words in _EVENT_RULES:
        hits = sum(1 for w in words if w in low)
        if hits > best_hits:
            best, best_hits = label, hits
    return best


def numbers(text: str) -> frozenset[str]:
    return frozenset(tok.replace(",", "").rstrip(".") for tok in _NUM_RE.findall(str(text or "")))


@dataclass(frozen=True)
class EventSig:
    """事件签名：谁 + 干了什么 + 多少钱（+ 拉丁专名，用于给宽松类型加一道证据）。"""

    entities: frozenset[str]
    etype: str
    numbers: frozenset[str] = frozenset()
    tokens: frozenset[str] = frozenset()
    title: str = ""
    brands: frozenset[str] = frozenset()

    def describe(self) -> str:
        who = "、".join(sorted(self.entities)[:3]) or "主体未识别"
        return f"{who} / {self.etype}"


# 事件类型越宽，「实体相同」就越不足以证明是同一件事：
# 「OpenAI 融资目标下调」是同一件事换数字，而「OpenAI 发布 GPT-6」与
# 「OpenAI 发布 Sora 3」是两件事。这三类必须再加一条证据（专名 / 数字 / 标题相似）。
_LOOSE_TYPES = frozenset({"模型发布", "技术研究"})


def latin_tokens(text: str) -> frozenset[str]:
    """拉丁专名 token（含品牌）：用于判断「是不是同一个产品/同一款芯片」。"""
    out: set[str] = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9]*(?:[-−.][A-Za-z0-9]+)*", str(text or "")):
        tok = m.group(0).lower().rstrip(".")
        if len(tok) < 3 or tok in _LATIN_STOP:
            continue
        out.add(tok)
    return frozenset(out)


def event_signature(text: str) -> EventSig:
    """只看标题：事件类型与实体都从标题取。

    实测教训：让摘要参与判定会引入两类噪声——
    · 摘要里顺带提到的「开源」会把一篇安全公告判成「模型发布」；
    · 摘要里的「无线通信半导体公司」会被当成主体，制造出两个标题共享的假实体。
    标题判不出类型的（「刚刚，OpenAI 又出手了」）就归「其他」，交给标题相似度闸门兜底。
    """
    from ..models import normalize_title

    head = str(text or "")
    ents = entities(head)
    return EventSig(
        ents,
        event_type(head),
        numbers(head),
        latin_tokens(head),
        normalize_title(head),
        frozenset(ents & _BRAND_CANONICAL),
    )


def same_event(a: EventSig, b: EventSig) -> bool:
    """同一事件：实体有交集 + 事件类型相同。数字不参与否决——

    「砍掉三分之一」与「砍掉 100 亿美元」的数字集合几乎不重合，但它们是同一件事；
    把数字当否决条件，恰好会放过最典型的重复。
    """
    from difflib import SequenceMatcher

    if not a.etype or a.etype == "其他" or a.etype != b.etype:
        return False
    shared = a.entities & b.entities
    if not shared:
        return False
    # 没有共同品牌时，只靠泛专名重合（「webgpu」「agent」这类技术词）不足以判同一事件，
    # 必须数字也对得上——否则两个不同的开源项目会被并成一条。
    if not (a.brands & b.brands) and not (a.numbers & b.numbers):
        return False
    # 双方都提到一堆主体、只重合一个：多半是「顺带提到」，要求数字上有交集才判重
    if len(shared) == 1 and max(len(a.entities), len(b.entities)) >= 3:
        return bool(a.numbers & b.numbers)
    # 两边各自点名了不同的对象/产品（OpenAI 与 ServiceNow 合作 vs OpenAI 与 Oracle 合作，
    # GPT-6 vs Sora）——那是两件事；只有数字能对上（同一笔钱/同一个数字）才继续判重。
    extra_a = a.tokens - shared
    extra_b = b.tokens - shared
    if extra_a and extra_b and not (extra_a & extra_b) and not (a.numbers & b.numbers):
        return False
    if a.etype in _LOOSE_TYPES:
        # 宽松类型再加一道证据：共享产品专名 / 数字有交集 / 标题本身高度相似
        if extra_a & extra_b or (a.numbers & b.numbers):
            return True
        return SequenceMatcher(None, a.title, b.title).ratio() >= 0.55
    return True


def item_signature(item: Item) -> EventSig:
    return event_signature(item.title)


# ---------------------------------------------------------------- 历史查重
def _as_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        # created_at 是本地时间入库的（datetime.now()），这里按本地时区补上
        dt = dt.astimezone()
    return dt.astimezone(timezone.utc)


def dedupe_events(
    items: list[Item],
    history: list[dict],
    days: int = 14,
    now: datetime | None = None,
) -> tuple[list[Item], list[dict]]:
    """候选 vs「最近出稿库」：同一事件直接拦截，不做「换个角度再写一篇」。

    返回 (保留条目, 拦截记录)。拦截记录带命中标题与时间，日志里能直接看出为什么被挡。
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    window = timedelta(days=max(int(days), 1))

    known: list[tuple[EventSig, dict]] = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        when = _as_dt(entry.get("created_at"))
        if when and (now - when) > window:
            continue
        sig = event_signature(title)
        if sig.etype == "其他" and not sig.entities:
            continue
        known.append((sig, {**entry, "title": title, "_when": when}))

    if not known:
        return items, []

    kept: list[Item] = []
    dropped: list[dict] = []
    for item in items:
        sig = item_signature(item)
        hit = next(((s, e) for s, e in known if same_event(sig, s)), None)
        if hit is None:
            kept.append(item)
            continue
        matched_sig, entry = hit
        when = entry.get("_when")
        dropped.append({
            "title": item.title,
            "event": sig.describe(),
            "matched_title": entry["title"],
            "matched_at": when.strftime("%Y-%m-%d") if when else "",
        })
    return kept, dropped


def collapse_same_event(items: list[Item]) -> tuple[list[Item], dict[str, list[Item]]]:
    """同一批候选里的同一事件合并成一条，其余报道挂到主条的 `same_event_sources`。

    「待出稿队列」内部的重复同样要挡：同一天的简报和快讯各推一条，读者看到的是
    两篇讲同一件事的稿子。合并后多篇报道只用于交叉核对事实，不额外成篇。
    """
    clusters: list[dict] = []
    for item in items:
        sig = item_signature(item)
        for cluster in clusters:
            if same_event(sig, cluster["sig"]):
                cluster["members"].append(item)
                break
        else:
            clusters.append({"sig": sig, "members": [item]})

    kept: list[Item] = []
    merged: dict[str, list[Item]] = {}
    for cluster in clusters:
        members: list[Item] = cluster["members"]
        rep = max(members, key=lambda x: (x.score, bool((x.extra or {}).get("full_text"))))
        others = [m for m in members if m is not rep]
        if others:
            sources = list((rep.extra or {}).get("same_event_sources") or [])
            seen_urls = {str(s.get("url") or "") for s in sources}
            for other in others:
                if other.url and other.url in seen_urls:
                    continue
                sources.append({
                    "title": other.title,
                    "source": other.source,
                    "url": other.url,
                    "summary": (other.summary or "")[:220],
                })
                seen_urls.add(other.url or "")
            rep.extra["same_event_sources"] = sources[:3]
            merged[rep.title] = others
        kept.append(rep)
    return kept, merged


def pick_subject(ranked: list[Item]) -> Item | None:
    """从排名前几的候选里挑「本期唯一主角」：优先有正文的一手报道。

    回抓正文成功（full_text）的条目才撑得起一篇产业解读；只有一句摘要的条目
    写出来全是空泛判断。都回抓失败时退到摘要更长的那条，最后才是第一名。
    """
    if not ranked:
        return None
    top = ranked[:3]
    with_text = [i for i in top if (i.extra or {}).get("full_text")]
    if with_text:
        return with_text[0]
    rich = [i for i in top if len((i.summary or "").strip()) >= 60]
    return (rich or top)[0]


def describe_sources(item: Item) -> str:
    """日志用：列出同一事件的其他报道，便于人工确认合并是否合理。"""
    extra = item.extra or {}
    names = [str(s.get("source") or "") for s in extra.get("same_event_sources") or []]
    return "、".join(n for n in names if n)
