"""论文元信息与正文主体的原子级绑定（防「张冠李戴」）。

事故背景（2026-11-14 那期《NEURIPS 2026 | DeLang-SR》）：
一期素材里有 3 篇论文，模型正文精读了 DeLang-SR，信息卡却填了同批第一篇
MUTE 的标题与作者——学术稿的信息卡是检索溯源的生命线，错位等于一票否决。

根因有三层，本模块负责后两层（前一层在 Prompt 里做目标锁定）：
1. 组稿时没有「本期就写这一篇」的硬绑定，模型可以自由在候选里挑主体；
2. 元信息卡兜底逻辑 `ensure_paper_meta_card(spec, selected[0])` 盲取列表第一条，
   模型正文写 A、兜底把它改成 B 的元数据 —— 越兜越错；
3. 成稿前没有一致性断言，错位稿一路走到排版与推送。

因此这里的判据全部是**确定性**的，不依赖模型自述：
- `title_signatures`：从论文标题里抽「可辨识代号」（DeLang-SR / PigMS / MUTE / SD3），
  并在候选之间做稀有性过滤（两篇标题都有的词不算代号）；
- `best_subject`：正文里到底在讲哪一篇（代号命中即可判定）；
- `best_meta_item`：信息卡的「论文名称」指向哪一篇（标题相似度）；
- `resolve_meta_subject`：两边不一致时以**正文主体**为准，并给出人话原因；
- `bind_paper_meta`：把三行元数据**逐字写回**目标论文的权威值；
  作者取不到时整行删除——宁可缺一行，绝不允许模型编人名。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..spec_tools import paper_meta_values

# 通用缩写/常见组合词：出现在几乎所有论文里，不能拿来判断「正文在讲哪一篇」
_GENERIC_ACRONYMS = {
    "cnn", "r-cnn", "gan", "llm", "vlm", "ai", "sota", "rgb", "gpu", "cpu", "iou", "map",
    "nerf", "vit", "vae", "lora", "api", "sdk", "ocr", "ssl", "mse", "psnr", "ssim", "lpips",
    "multi-level", "multi-scale", "real-world", "zero-shot", "few-shot", "state-of-the-art",
    "end-to-end", "self-supervised", "semi-supervised", "large-scale", "high-resolution",
    "low-light", "low-level", "talking-head", "text-to-image", "text-to-video",
    "super-resolution", "point-cloud", "neural-network", "deep-learning",
}
_GENERIC_WORDS = {"multi", "level", "real", "world", "zero", "shot", "few", "state", "art",
                  "end", "self", "semi", "large", "scale", "high", "low", "light", "talking",
                  "head", "text", "image", "video", "super", "resolution", "point", "cloud",
                  "neural", "network", "deep", "learning", "based", "towards"}
_TOKEN_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z\-]*")
_TITLE_MATCH_MIN = 0.72     # 信息卡标题与候选标题的最低相似度
_SUBJECT_SCORE_MIN = 3      # 正文至少要命中一个代号（权重 3）才算「指向」某篇


# ---------------------------------------------------------------- 代号抽取
def title_signatures(title: str) -> list[str]:
    """从论文标题里抽可辨识代号：DeLang-SR / PigMS / MUTE / SD3 / R-CNN(过滤掉)。"""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(title or ""):
        t = tok.strip("-")
        if len(t) < 3:
            continue
        caps = t.isupper()
        camel = any(c.isupper() for c in t[1:]) and any(c.islower() for c in t)
        hyphen = "-" in t and any(c.isupper() for c in t)
        if not (caps or camel or hyphen):
            continue
        if t.lower() in _GENERIC_ACRONYMS:
            continue
        if "-" in t and all(p.lower() in _GENERIC_WORDS for p in t.split("-")):
            continue
        if t not in out:
            out.append(t)
    # 标题里「某代号: 副标题」的写法最可信，排到最前
    head = str(title or "").split(":", 1)[0]
    out.sort(key=lambda s: (s not in head, -len(s)))
    return out


def _title_words(title: str) -> list[str]:
    """次要信号：标题里的长英文实词（中文正文命中率低，只作分数加成）。"""
    return [w.lower() for w in _TOKEN_RE.findall(title or "")
            if len(w) >= 7 and w.isalpha() and w.lower() not in _GENERIC_WORDS]


def item_signatures(item) -> dict[str, float]:
    """一篇论文的「自称代号」及可信度权重。

    arXiv 标题常常只写方法全称（Decoupling Direction and Magnitude: …），
    代号 DeLang-SR 反而只出现在**仓库名**与**摘要**里（实测 2026-11-14 那期
    就是靠仓库名才对上）。因此按信源可信度加权：标题 / 仓库名 1.0、
    图注 0.4、摘要 0.2——弱信号单条不足以定性，但多条的合力可以。
    """
    sigs: dict[str, float] = {}

    def add(text: str, weight: float) -> None:
        for s in title_signatures(text or ""):
            sigs[s] = max(sigs.get(s, 0.0), weight)

    add(getattr(item, "title", ""), 1.0)
    code = str(getattr(item, "code_url", "") or "").rstrip("/")
    if code:
        add(code.rsplit("/", 1)[-1].replace("_", "-"), 1.0)   # github.com/u/DeLang-SR
    for key in ("figure_caption", "figure_caption_2"):
        add(str((getattr(item, "extra", None) or {}).get(key) or ""), 0.4)
    add(str(getattr(item, "summary", "") or "")[:1200], 0.2)
    return sigs


def _basename(path: str) -> str:
    return str(path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()


def _spec_images(spec: dict) -> list[str]:
    out = []
    for blk in (spec or {}).get("sections") or []:
        if isinstance(blk, dict) and blk.get("type") == "figure":
            if blk.get("src"):
                out.append(str(blk["src"]))
    return out


def _image_hits(spec: dict, items: list) -> list[int]:
    """正文配图来自哪一篇：图源文件名与候选论文的 images 对上，是最硬的证据。"""
    want = {_basename(s) for s in _spec_images(spec) if _basename(s)}
    hits = []
    for item in items:
        own = {_basename(u) for u in (getattr(item, "images", None) or [])}
        if getattr(item, "image_url", ""):
            own.add(_basename(item.image_url))
        hits.append(len(want & own))
    return hits


def _rarity_filter(per_item: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """两个候选都带的代号不具区分度（如 SD3、Restormer），直接丢掉。"""
    seen: dict[str, int] = {}
    for sigs in per_item.values():
        for s in sigs:
            seen[s.lower()] = seen.get(s.lower(), 0) + 1
    return {key: {s: w for s, w in sigs.items() if seen.get(s.lower(), 0) <= 1}
            for key, sigs in per_item.items()}


# ---------------------------------------------------------------- 正文文本
def spec_body_text(spec: dict) -> str:
    """正文全量文本（标题 + 副标题 + 导读 + 各节 + 一句话总结）。"""
    if not isinstance(spec, dict):
        return ""
    parts = [str(spec.get("title") or ""), str(spec.get("subtitle") or ""),
             str(spec.get("takeaway") or "")]
    parts.extend(str(p) for p in (spec.get("lead") or []))
    for blk in spec.get("sections") or []:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "meta_card":
            continue
        for key in ("text", "caption", "src"):
            if blk.get(key):
                parts.append(str(blk[key]))
        for b in blk.get("bullets") or []:
            parts.append(str(b))
        for row in blk.get("rows") or []:
            if isinstance(row, (list, tuple)):
                parts.extend(str(c) for c in row)
    return "\n".join(p for p in parts if p)


def _score(text: str, sigs: dict[str, float], words: list[str]) -> float:
    low = (text or "").lower()
    if not low:
        return 0.0
    return sum(w * 3 for s, w in sigs.items() if s.lower() in low) \
        + sum(1 for w in words if w in low)


def best_subject(spec: dict, items: list) -> tuple[object | None, dict[str, float]]:
    """正文指向哪一篇：返回 (item | None, {标题前 40 字: 得分})。得分并列时判为「无法判定」。

    证据来源依次加权：正文里出现该论文**配图**（+3/张，最硬）> 标题/仓库名代号（×3）
    > 图注代号（×1.2）> 摘要代号（×0.6）> 标题长词（×1）。
    """
    items = [i for i in items if i is not None]
    if not items:
        return None, {}
    text = spec_body_text(spec)
    raw = {str(idx): item_signatures(item) for idx, item in enumerate(items)}
    sigs_map = _rarity_filter(raw)
    images = _image_hits(spec, items)
    scored: list[tuple[object, float]] = []
    for idx, item in enumerate(items):
        score = _score(text, sigs_map.get(str(idx), {}), _title_words(getattr(item, "title", "")))
        score += 3.0 * images[idx]
        scored.append((item, score))
    best_item, best_score = max(scored, key=lambda p: p[1])
    report = {getattr(it, "title", "")[:40]: round(s, 2) for it, s in scored}
    if best_score < _SUBJECT_SCORE_MIN:
        return None, report
    if sum(1 for _, s in scored if s == best_score) > 1:
        return None, report          # 多篇同分 → 不作判定，交给调用方回退
    return best_item, report


def title_points_elsewhere(spec: dict, items: list, target) -> object | None:
    """标题里的方法代号是否指向了**另一篇**候选（信息卡对了、标题却串台的兜底检查）。

    只做告警不做改写：标题是人写的门面，改错比不改更糟——由运营看到告警后决定。
    """
    if target is None:
        return None
    title = f"{spec.get('title') or ''} {spec.get('subtitle') or ''}"
    own = _score(title, item_signatures(target), _title_words(getattr(target, "title", "")))
    best_other, best_score = None, 0.0
    for item in items:
        if item is target:
            continue
        sc = _score(title, item_signatures(item), _title_words(getattr(item, "title", "")))
        if sc > best_score:
            best_other, best_score = item, sc
    if best_other is not None and best_score >= _SUBJECT_SCORE_MIN and best_score > own:
        return best_other
    return None


# ---------------------------------------------------------------- 信息卡指向
def meta_title(spec: dict) -> str:
    if not isinstance(spec, dict):
        return ""
    for blk in spec.get("sections") or []:
        if isinstance(blk, dict) and blk.get("type") == "meta_card":
            for row in blk.get("rows") or []:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    if str(row[0]).strip() in ("论文名称", "论文标题", "论文题目", "论文名", "标题"):
                        return str(row[1]).strip()
    return ""


def _similarity(a: str, b: str) -> float:
    raw_a, raw_b = str(a or "").strip(), str(b or "").strip()
    if not raw_a or not raw_b:
        return 0.0
    if raw_a.lower() == raw_b.lower():
        return 1.0
    ratio = SequenceMatcher(None, raw_a.lower(), raw_b.lower()).ratio()
    # 代号完全一致（「MUTE: ...」vs「MUTE」/ 英文原名 vs 中文标题）也算强匹配。
    # 注意：必须在**原始字符串**上抽代号——先小写化会废掉驼峰识别（MixCompress / SVI360）
    sig_a = {s.lower() for s in title_signatures(raw_a)}
    sig_b = {s.lower() for s in title_signatures(raw_b)}
    if sig_a & sig_b:
        ratio = max(ratio, 0.8)
    return ratio


def best_meta_item(spec: dict, items: list) -> tuple[object | None, float]:
    """信息卡的「论文名称」指向哪一篇（含相似度）。"""
    declared = meta_title(spec)
    if not declared:
        return None, 0.0
    best, best_ratio = None, 0.0
    for item in items:
        r = _similarity(declared, getattr(item, "title", ""))
        if r > best_ratio:
            best, best_ratio = item, r
    return (best if best_ratio >= _TITLE_MATCH_MIN else None), best_ratio


# ---------------------------------------------------------------- 组装判据
def resolve_meta_subject(spec: dict, items: list) -> tuple[object | None, dict]:
    """确定「信息卡必须绑定的那一篇」，并返回可读的体检记录。

    优先级：正文主体 > 信息卡自述指向 > 候选第一名（pipeline 已按分数排序）。
    返回 `(target_item, info)`，info 里带 mismatch / reason，供日志与告警使用。
    """
    items = [i for i in items if i is not None]
    if not items:
        return None, {"mismatch": False, "reason": "无候选论文"}
    body_item, scores = best_subject(spec, items)
    declared_item, ratio = best_meta_item(spec, items)
    focus = items[0]
    target = body_item or declared_item or focus
    reason = ""
    if body_item is not None and declared_item is not None and body_item is not declared_item:
        reason = (f"信息卡写的是《{getattr(declared_item, 'title', '')[:44]}》，"
                  f"正文讲的是《{getattr(body_item, 'title', '')[:44]}》")
    elif declared_item is None and meta_title(spec):
        reason = f"信息卡标题《{meta_title(spec)[:44]}》与本期任何候选都对不上（疑似编造）"
    elif body_item is None and declared_item is None:
        reason = "正文与信息卡都无法指向具体候选"
    info = {
        "mismatch": bool(reason),
        "reason": reason,
        "scores": scores,
        "meta_similarity": round(ratio, 3),
        "meta_item": getattr(declared_item, "title", "") if declared_item else "",
        "body_item": getattr(body_item, "title", "") if body_item else "",
        "target": getattr(target, "title", ""),
    }
    return target, info


# ---------------------------------------------------------------- 强制绑定
def bind_paper_meta(spec: dict, item) -> dict:
    """把信息卡三行元数据**逐字写回**目标论文的权威值。

    论文名称 / 发表期刊/会议 / 作者团队 一律来自 `paper_meta_values(item)`，
    即目标论文自身的标题、venue 与作者列表；作者缺失时删除该行而不是编人名。
    返回 `{"fixed": n, "removed": [...], "values": {...}}`。
    """
    values = paper_meta_values(item) if item is not None else {}
    if not values.get("论文名称"):
        return {"fixed": 0, "removed": [], "values": {}}
    sections = spec.get("sections")
    if not isinstance(sections, list):
        return {"fixed": 0, "removed": [], "values": values}
    card = next((b for b in sections if isinstance(b, dict) and b.get("type") == "meta_card"), None)
    if card is None:      # 没卡交给 ensure_paper_meta_card 建，避免两处都插入
        return {"fixed": 0, "removed": [], "values": values}

    from ..spec_tools import _meta_kind          # 标签归一与补卡逻辑共用一套口径

    rows = [list(r) for r in (card.get("rows") or [])
            if isinstance(r, (list, tuple)) and len(r) >= 2]
    fixed, removed = 0, []
    for canonical in ("论文名称", "发表期刊/会议", "作者团队"):
        truth = values.get(canonical) or ""
        found = next((r for r in rows if _meta_kind(str(r[0])) == canonical), None)
        if not truth:
            # 取不到权威值：宁可整行删掉，也不保留模型可能编造的内容
            if found is not None:
                rows.remove(found)
                removed.append(canonical)
                fixed += 1
            continue
        if found is None:
            rows.append([canonical, truth])
            fixed += 1
        elif str(found[1]).strip() != truth:
            found[1] = truth
            fixed += 1
    if fixed:
        card["rows"] = rows
        if not card.get("label"):
            card["label"] = "PAPER INFO"
    return {"fixed": fixed, "removed": removed, "values": values}


def assert_paper_meta(spec: dict, item) -> list[str]:
    """成稿前的一致性断言：返回仍未对齐的问题项（空列表 = 通过）。

    正常路径下 `bind_paper_meta` 之后这里必然为空；留下这道断言是为了
    万一绑定失效（例如信息卡结构被模型改成别的形态）宁可熔断也不发错位稿。
    """
    if item is None:
        return []
    values = paper_meta_values(item)
    problems: list[str] = []
    declared = meta_title(spec)
    if values.get("论文名称") and declared and _similarity(declared, values["论文名称"]) < 0.95:
        problems.append(f"信息卡论文名称「{declared[:40]}」≠ 目标论文「{values['论文名称'][:40]}」")
    if values.get("作者团队"):
        want = values["作者团队"]
        card_text = ""
        for blk in spec.get("sections") or []:
            if isinstance(blk, dict) and blk.get("type") == "meta_card":
                for row in blk.get("rows") or []:
                    if isinstance(row, (list, tuple)) and len(row) >= 2 and _label_is_author(str(row[0])):
                        card_text = str(row[1])
        if not card_text:
            problems.append("信息卡缺「作者团队」行")
        else:
            want_names = re.findall(r"[\u4e00-\u9fa5]{2,4}|[A-Z][A-Za-z\.\-']{2,}", want)
            missing = [n for n in want_names if n not in card_text and n != "由"]
            if len(missing) >= max(2, len(want_names) // 2):
                problems.append(f"信息卡作者与目标论文作者不符（缺 {len(missing)} 位）")
    return problems


def _label_is_author(label: str) -> bool:
    return any(k in label for k in ("作者", "团队"))
