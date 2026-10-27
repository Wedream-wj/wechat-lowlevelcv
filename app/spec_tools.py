"""ArticleSpec 的后处理工具：章节标题/编号规范化 + 移动端表格列压缩。

为什么单独一个模块：这两件事都是「成稿之后的规格修正」，
而且要在三个环节共用——
  1. 组稿前：素材里的 9 列实验表先压成 5 列再喂给模型，模型才不会照抄超宽表；
  2. 成稿后：模型自己也可能拼出超宽表、或因省略某节留下编号断层，代码层强制修正；
  3. 排版前：渲染层再兜一次底，保证发出去的 HTML 里 SECTION 永远是 01,02,03…

设计原则：**只动结构，不动数字**。压缩表格只删列，不合并、不改写任何单元格，
避免把论文原文的数字改错。
"""
from __future__ import annotations

import re

# 手机端公众号正文可视宽度约 340–360pt：一列方法名 + 4 列指标是舒适上限，
# 再多就要横向滚动，阅读体验会明显变差。
MAX_TABLE_COLS = 5

# ------------------------------------------------------------------ 章节编号
# 只认「01 | 标题」「1｜标题」这类带编号的 h2；序号位数不限制，统一重排为两位。
_H2_NUM_RE = re.compile(r"^(\d{1,2})\s*[|｜]\s*(.+)$")

# 章节标题的正式叫法 ← 历史叫法/口语叫法。只收敛「同一节的不同写法」，
# 不做语义改写：栏目对收尾节的正式命名是「写在最后」，早期的「冷静一下」
# 偏口语，且容易让模型把这一节写成零散质疑而不是终局判断，因此统一归到正式名。
_SECTION_TITLE_ALIASES: dict[str, str] = {
    "冷静一下": "写在最后",
    "冷静想想": "写在最后",
    "冷静一点": "写在最后",
    "冷静看待": "写在最后",
    "写在结尾": "写在最后",
    "结语": "写在最后",
    "尾声": "写在最后",
}

# 标题可能带副标题（「冷静一下：信息边界」），比对时只取主干
_TITLE_HEAD_RE = re.compile(r"[：:（(【\[—–\-/|｜]")


def _numbered_h2(block: dict) -> re.Match | None:
    if not isinstance(block, dict) or block.get("type") != "h2":
        return None
    return _H2_NUM_RE.match(str(block.get("text") or "").strip())


def normalize_section_titles(spec: dict) -> int:
    """把 h2 标题里的历史叫法收敛到正式名，返回被改写的节数。

    编号原样保留（编号连续性由 renumber_sections 负责），只改标题文字。
    若全篇已经有一节叫正式名，则不再把别的节改成同名，避免出现两个「写在最后」。
    """
    blocks = [b for b in (spec.get("sections") or []) if _numbered_h2(b)]
    taken = {m.group(2).strip() for m in (_numbered_h2(b) for b in blocks) if m}
    changed = 0
    for block in blocks:
        m = _numbered_h2(block)
        title = m.group(2).strip()
        head = _TITLE_HEAD_RE.split(title)[0].strip()
        canon = _SECTION_TITLE_ALIASES.get(title) or _SECTION_TITLE_ALIASES.get(head)
        if not canon or title == canon or canon in taken:
            continue
        block["text"] = f"{m.group(1)} | {canon}"
        taken.add(canon)
        changed += 1
    return changed


def renumber_sections(spec: dict) -> bool:
    """把 h2 小节的编号按出现顺序重排为 01,02,03…，消除跳号与重号。

    实测触发场景：单篇论文素材时「05 | 总结」按规则省略，模型却仍把最后一节写成
    「06 | 写在最后」，正文里就出现 04 → 06 的断层。这里不做任何语义判断，
    只认「第几个带编号的小节」——顺序对了，编号必然连续。
    返回是否发生了修改。
    """
    sections = spec.get("sections") or []
    if not any(_numbered_h2(b) for b in sections):
        return False
    changed = False
    seq = 0
    for block in sections:
        m = _numbered_h2(block)
        if not m:
            continue
        seq += 1
        new_text = f"{seq:02d} | {m.group(2).strip()}"
        if str(block.get("text") or "").strip() != new_text:
            block["text"] = new_text
            changed = True
    return changed


def normalized_spec(spec: dict, module: str | None = None) -> dict:
    """返回「标题已规范、编号已连续、信息卡已收敛」的浅拷贝（不改写调用方的 spec）。

    module 传业界时会顺带把产业事件卡的主体/事件行合并成一行——渲染层与成稿出口
    共用这一份逻辑，避免预览与推文长得不一样。
    """
    sections = spec.get("sections") or []
    needs_numbering = any(_numbered_h2(b) for b in sections)
    needs_cards = module == "industry" and any(
        isinstance(b, dict) and b.get("type") == "meta_card" for b in sections)
    if not needs_numbering and not needs_cards:
        return spec
    clone = dict(spec)
    clone["sections"] = [dict(b) if isinstance(b, dict) else b for b in sections]
    if needs_cards:
        normalize_meta_cards(clone, module)
    normalize_section_titles(clone)
    renumber_sections(clone)
    return clone


# ------------------------------------------------------------------ 信息卡
CARD_LABELS = ("PAPER INFO", "PROJECT INFO", "INDUSTRY SNAPSHOT", "CONTEST INFO")

# 行内容出现这些词，说明主体是仓库/工具链，卡片标题该是 PROJECT INFO
# 注意「权重」不能单独作特征词：产业新闻里「开放权重」是高频说法，
# 会把「Runway 开源开放权重模型」这类事件卡误判成开源项目卡
_PROJECT_HINTS = (
    "github", "repo", "仓库", "开源", "stars", "star", "星标", "权重文件", "huggingface",
    "hugging face", "license", "许可", "sdk", "工具链", "项目地址", "代码仓库", "显存",
)

# 产业事件卡的行名。主体与事件原本分两行写（「主体：Runway」「事件：发布 Praxis-1」），
# 第一行只有一个公司名、第二行才见动作，卡片密度被浪费；规模/金额再单独占一行更散。
# 现在统一并成「事件：Runway 发布 Praxis-1」，金额折进括号里。
_SUBJECT_LABELS = ("主体", "公司", "机构", "厂商", "团队", "发布方", "事件主体", "当事方")
_EVENT_LABELS = ("事件", "动作", "发生了什么", "发布内容", "合作内容", "核心事件")
_AMOUNT_HINTS = ("规模", "金额", "融资", "估值", "交易额", "营收")


def resolve_card_label(block: dict, module: str) -> str:
    """信息卡标题自适应：论文 / 开源项目 / 产业快照 / 赛事。

    为什么不能写死 PAPER INFO：业界稿里一张「GitHub 仓库 + stars + 许可证」的卡
    顶着 PAPER INFO，读者第一眼就出戏。模型显式给了合法 label 就照用；
    否则按模块 + 行内容兜底——行里出现仓库/星标/许可证这类词，判为 PROJECT INFO。
    """
    explicit = str(block.get("label") or "").strip().upper()
    if explicit in CARD_LABELS:
        return explicit
    rows = [r for r in (block.get("rows") or []) if isinstance(r, (list, tuple)) and len(r) >= 2]
    text = " ".join(f"{r[0]} {r[1]}" for r in rows).lower()
    # 「主体 / 事件 / 规模」这种行结构本身就说明是产业事件卡：
    # 事件里出现「开源」「权重」的词很常见，不能因此判成开源项目卡
    is_event_card = any(_row_label(r) in _SUBJECT_LABELS + _EVENT_LABELS for r in rows)
    is_project = (not is_event_card) and any(hint in text for hint in _PROJECT_HINTS)

    if module == "industry":
        return "PROJECT INFO" if is_project else "INDUSTRY SNAPSHOT"
    if module == "paper":
        return "PAPER INFO"
    if module == "contest":
        return "CONTEST INFO"
    return "PROJECT INFO" if is_project else "PAPER INFO"


def _row_label(row) -> str:
    return str(row[0] or "").strip().rstrip("：:")


def _is_amount_label(label: str) -> bool:
    return any(hint in label for hint in _AMOUNT_HINTS)


def _join_subject_event(subject: str, event: str) -> str:
    """「Runway」+「发布 Praxis-1」→「Runway 发布 Praxis-1」。

    英文主体与中文动作之间补一个空格：不补会出现「Runway发布」，读起来像拼写错误。
    """
    a, b = str(subject or "").strip(), str(event or "").strip()
    if not a:
        return b
    if not b or b.startswith(a):
        return b or a
    if a[-1].isascii() and a[-1].isalnum() and not b[0].isascii():
        return f"{a} {b}"
    return f"{a}{b}"


def merge_event_rows(rows: list) -> tuple[list, bool]:
    """把产业事件卡合并成「事件」单行，去掉独立的规模/金额行。

    只处理「主体 + 事件」这种两行结构；金额/规模行的数字不丢，折进合并句的括号里
    （「Praxis-1 开放权重（0.95 相关性）」），读者照样看得到量级。
    """
    valid = [list(r) for r in (rows or []) if isinstance(r, (list, tuple)) and len(r) >= 2]
    if not valid:
        return rows, False
    subject = next((r for r in valid if _row_label(r) in _SUBJECT_LABELS), None)
    event = next((r for r in valid if _row_label(r) in _EVENT_LABELS), None)
    amounts = [r for r in valid if _is_amount_label(_row_label(r))]
    if not amounts and not (subject and event):
        return rows, False

    base = ""
    if subject and event:
        base = _join_subject_event(subject[1], event[1])
    elif event:
        base = str(event[1]).strip()
    if not base:
        return rows, False

    merged = base
    for row in amounts:
        value = str(row[1] or "").strip()
        if value and value not in merged:
            merged = f"{merged}（{value}）"

    out: list = []
    placed = False
    for row in valid:
        label = _row_label(row)
        if _is_amount_label(label):
            continue
        if row is subject or row is event:
            if not placed:
                out.append(["事件", merged])
                placed = True
            continue
        out.append(row)
    if not placed:
        return rows, False
    return out, True


def normalize_meta_cards(spec: dict, module: str) -> int:
    """成稿兜底：产业事件卡合并主体/事件行、去掉规模行。返回被改动的卡数。"""
    if module != "industry":
        return 0
    changed = 0
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") != "meta_card":
            continue
        if resolve_card_label(block, module) != "INDUSTRY SNAPSHOT":
            continue  # 开源项目卡（PROJECT INFO）保留原有字段，不参与合并
        new_rows, ok = merge_event_rows(block.get("rows") or [])
        if ok:
            block["rows"] = new_rows
            changed += 1
    return changed


# ------------------------------------------------------------------ 表格压缩
# 指标字典：关键词 → (类别, 类别内优先级)。类别用于「精度 / 误差 / 成本」轮转取样，
# 保证压缩后的表同时覆盖效果与代价，而不是留下一堆同质指标。
_METRIC_ORDER: dict[str, tuple[str, int]] = {
    # 保真与感知质量
    "psnr": ("quality", 0),
    "ws-psnr": ("quality", 1),
    "ssim": ("quality", 2),
    "ws-ssim": ("quality", 3),
    "ms-ssim": ("quality", 4),
    "lpips": ("quality", 5),
    "dists": ("quality", 6),
    "fid": ("quality", 7),
    "maniqa": ("quality", 8),
    "musiq": ("quality", 9),
    "clipiqa": ("quality", 10),
    "niqe": ("quality", 11),
    "vmaf": ("quality", 12),
    "hdr-vdp2": ("quality", 13),
    # 几何/重建误差（球面/插帧/光流类论文的核心指标）
    "sepe": ("error", 0),
    "epe": ("error", 1),
    "ae": ("error", 2),
    "mae": ("error", 3),
    "rmse": ("error", 4),
    "mse": ("error", 5),
    # 计算代价
    "latency": ("cost", 0),
    "fps": ("cost", 1),
    "params": ("cost", 2),
    "param": ("cost", 3),
    "flops": ("cost", 4),
    "gflops": ("cost", 5),
    "memory": ("cost", 6),
    "bpp": ("cost", 7),
    "time": ("cost", 8),
    "runtime": ("cost", 9),
}
# 长关键词优先匹配："ws-psnr" 必须先于 "psnr" 命中，否则两个指标会混成同一类同一档
_METRIC_KEYS = sorted(_METRIC_ORDER, key=len, reverse=True)
_CATEGORY_CYCLE = ("quality", "error", "cost")

_OURS_RE = re.compile(r"(?:ours|proposed|our\s+method|本文|本方法)", re.I)
_NUM_CELL_RE = re.compile(
    r"^\s*[~≈<>≤≥]?\s*[-+]?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*±\s*\d+(?:\.\d+)?)?\s*[%]?\s*$"
)
_ARROW_RE = re.compile(r"[↑↓⇑⇓▲▼]")


def _norm(text: str) -> str:
    return _ARROW_RE.sub("", str(text or "")).lower().replace("（", "(").strip()


def _metric(text: str) -> tuple[str, int] | None:
    """表头/行名 → (类别, 档位)。认不出返回 None（视为文本列）。"""
    norm = _norm(text)
    if not norm:
        return None
    for kw in _METRIC_KEYS:
        if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", norm):
            return _METRIC_ORDER[kw]
    return None


def _is_numeric_cell(cell: str) -> bool:
    return bool(_NUM_CELL_RE.match(str(cell or "")))


def _normalize(table: dict) -> tuple[list[str], list[list[str]]]:
    """表头定宽：行不足补空、超出截断，保证 header 与每一行等长。"""
    header = [str(h or "") for h in (table.get("header") or [])]
    rows = [[str(c or "") for c in (r or [])] for r in (table.get("rows") or [])]
    if not header:
        return header, rows
    width = len(header)
    rows = [(r + [""] * width)[:width] for r in rows]
    return header, rows


def _is_metric_table(header: list[str], rows: list[list[str]]) -> bool:
    """是否是「实验指标对比表」。

    两种形态都算：
    - 常规表：表头里直接是 PSNR/SSIM/Latency 等指标名；
    - 转置表：首列是指标名（Dataset/Metric + 指标行），表头是方法名。
    只有这类表才允许裁列——赛事时间线、数据集统计之类的文本表裁了会丢信息。
    """
    if sum(1 for h in header if _metric(h)) >= 2:
        return True
    # 转置表：指标名写在左侧一到两列的行标签里（Dataset | Metrics | 各方法）
    per_row_hits = sum(1 for r in rows if any(_metric(c) for c in r[:2]))
    if per_row_hits >= 2:
        return True
    return bool(_metric(header[0])) and len(header) > MAX_TABLE_COLS


def _label_columns(header: list[str], rows: list[list[str]], limit: int = 2) -> list[int]:
    """从左侧识别标签列（方法名/Dataset 这类文本列），最多保留 limit 列。"""
    labels: list[int] = []
    for ci, h in enumerate(header):
        if len(labels) >= limit:
            break
        if _metric(h):
            break
        values = [r[ci] for r in rows if ci < len(r)]
        numeric = sum(1 for v in values if v and _is_numeric_cell(v))
        if values and numeric * 2 > len(values):  # 多数单元格是数字 → 不是标签列
            break
        labels.append(ci)
    return labels or [0]


def _round_robin(metrics: list[tuple[int, str, int]], budget: int) -> list[int]:
    """按「质量 → 误差 → 成本」轮转取指标，让一张表同时说清效果与代价。"""
    buckets: dict[str, list[tuple[int, int]]] = {c: [] for c in _CATEGORY_CYCLE}
    for ci, cat, rank in metrics:
        buckets[cat].append((rank, ci))
    for cat in buckets:
        buckets[cat].sort()
    picks: list[int] = []
    while len(picks) < budget and any(buckets.values()):
        for cat in _CATEGORY_CYCLE:
            if buckets[cat] and len(picks) < budget:
                picks.append(buckets[cat].pop(0)[1])
    return picks


def _pick_columns(header: list[str], rows: list[list[str]]) -> list[int] | None:
    """返回要保留的列下标；无需改动时返回 None。"""
    n = len(header)
    useful = [ci for ci in range(n)
              if header[ci].strip() or any(r[ci].strip() for r in rows)]
    if len(useful) <= MAX_TABLE_COLS and len(useful) == n:
        return None
    if len(useful) <= MAX_TABLE_COLS:
        return useful  # 只是去掉了几列全空列

    labels = _label_columns(header, rows)
    metric_cols: list[tuple[int, str, int]] = []
    for ci in useful:
        if ci in labels:
            continue
        parsed = _metric(header[ci])
        if parsed:
            metric_cols.append((ci, parsed[0], parsed[1]))
    budget = max(1, MAX_TABLE_COLS - len(labels))

    if metric_cols:
        keep = set(labels) | set(_round_robin(metric_cols, budget))
        return sorted(keep)

    # 转置/方法对比表：表头是方法名，保标签列 + 本文方法列 + 最右侧（最新 SOTA）邻居
    others = [ci for ci in useful if ci not in labels]
    ours = [ci for ci in others if _OURS_RE.search(header[ci])]
    picks = list(ours[:1]) or ([others[-1]] if others else [])
    for ci in reversed(others):
        if len(picks) >= budget:
            break
        if ci not in picks:
            picks.append(ci)
    return sorted(set(labels) | set(picks))


def compact_table(table: dict) -> dict:
    """按移动端宽度压列。非指标对比表原样返回（只做等宽规整）。"""
    header, rows = _normalize(table)
    if not header:
        return table
    if not _is_metric_table(header, rows):
        if header == table.get("header") and rows == table.get("rows"):
            return table
        out = dict(table)
        out["header"], out["rows"] = header, rows
        return out
    keep = _pick_columns(header, rows)
    if keep is None or len(keep) == len(header):
        if header == table.get("header") and rows == table.get("rows"):
            return table
        out = dict(table)
        out["header"], out["rows"] = header, rows
        return out
    out = dict(table)
    out["header"] = [header[ci] for ci in keep]
    out["rows"] = [[r[ci] for ci in keep] for r in rows]
    return out


def compact_tables(tables: list[dict]) -> list[dict]:
    return [compact_table(t) for t in tables or [] if isinstance(t, dict)]


def prune_spec_tables(spec: dict) -> int:
    """成稿兜底：把 spec 里所有实验表压到 MAX_TABLE_COLS 列，返回被改动张数。"""
    changed = 0
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") != "table":
            continue
        new = compact_table(block)
        if new is block:
            continue
        if new.get("header") != block.get("header") or new.get("rows") != block.get("rows"):
            block["header"] = new.get("header") or []
            block["rows"] = new.get("rows") or []
            changed += 1
    return changed
