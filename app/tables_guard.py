"""实验表格「本文方法零留白」守卫：解析检测 → 成稿断言 → 按原文回填。

铁律（2026-11-16 事故整改）：
1. 表格里代表本文方法的那一行（Ours / 本文 / 方法代号）必须整行有数；
   全空、全「—」、只有占位符，一律视为残缺表，禁止出稿。
2. 回填只允许取原文表里的数值——按「基准 + 指标」定位单元格，逐字照抄，
   不做任何换算、插值或猜测；找不到就整表撤掉，改成文字交代原文表格。
3. 解析侧同一条线：表头有「+Ours」列、列里却一个数都没有，说明表已经错位，
   这种表不许喂给模型（模型会照着补一行「+Ours | — | —」）。

本模块不依赖 app 内其它模块，供 arxiv 解析、pipeline 成稿、编辑器保存三处共用。
"""
from __future__ import annotations

import math
import re

# 「没有数」的单元格写法：空串、破折号、斜杠、N/A、问号、中文占位词
_BLANK_TOKENS = {
    "", "-", "—", "–", "−", "――", "/", "\\", "n/a", "na", "none", "null",
    "?", "？", "待补", "未知", "无", "暂无", "— —",
}
_ARROW_RE = re.compile(r"[↑↓⇑⇓▲▼]")
_CITE_RE = re.compile(r"\[\s*\d+(?:\s*[,，–-]\s*\d+)*\s*\]")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
_NUM_ONLY_RE = re.compile(r"^[~≈<>≤≥]?\s*[-+]?\d[\d,]*(?:\.\d+)?(?:\s*±\s*\d+(?:\.\d+)?)?\s*%?\s*$")
# 指标词表：长词优先（ws-psnr 必须先于 psnr 命中，否则两列会并成一列）
METRIC_KEYS = sorted(
    (
        "ws-psnr", "ms-ssim", "ws-ssim", "hdr-vdp2", "bd-rate", "clipiqa", "maniqa",
        "musiq", "dists", "lpips", "psnr", "ssim", "niqe", "vmaf", "sepe", "rmse",
        "mae", "mse", "epe", "f1", "plcc", "srcc", "krcc", "miou", "map", "iou",
        "accuracy", "precision", "recall", "params", "param", "flops", "gflops",
        "latency", "runtime", "fps", "bpp", "psnr-b", "time", "memory",
    ),
    key=len, reverse=True,
)
_METRIC_SET = set(METRIC_KEYS)
# 方法代号的误判词：标题里的 venue、连接词不能当方法名
_STOP_TOKENS = {
    "cvpr", "iccv", "eccv", "neurips", "nips", "icml", "iclr", "aaai", "ijcai",
    "tpami", "tip", "pr", "kbs", "icme", "acm", "mm", "the", "a", "an", "towards",
    "toward", "via", "with", "for", "and", "of", "on", "in", "is", "are", "towards",
    "efficient", "learning", "unified", "rethinking", "beyond", "simple",
}


# ---------------------------------------------------------------- 基础判定
def is_blank(cell) -> bool:
    """单元格是否「没有数据」：空串 / 破折号 / N/A / 问号 / 中文占位词。"""
    text = str(cell or "").strip()
    if text.lower() in _BLANK_TOKENS:
        return True
    # 「— —」「- / -」这类纯符号组合也算空
    return bool(text) and not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text)


def clean_label(text: str) -> str:
    """标签归一：去引用角标、去箭头、压空白、转小写。"""
    text = _CITE_RE.sub("", str(text or ""))
    text = _ARROW_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .:：,，;；|").lower()


def _num_of(text: str) -> str:
    """单元格里的第一个数值（保留百分号），取不到返回空串。"""
    text = str(text or "").replace(",", "")
    m = _NUM_RE.search(text)
    if not m:
        return ""
    value = m.group(0)
    tail = text[m.end():m.end() + 1]
    return value + ("%" if tail == "%" else "")


def split_metric(text: str) -> tuple[str, str]:
    """「Set5 PSNR ↑」→ ("set5", "psnr")；「PSNR」→ ("", "psnr")。

    指标词取最长匹配，基准名是指标词之前的部分（去箭头与标点）。
    """
    raw = _CITE_RE.sub("", str(text or ""))
    norm = clean_label(raw)
    if not norm:
        return "", ""
    for key in METRIC_KEYS:
        m = re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", norm)
        if m:
            bench = re.sub(r"[\s\-_/|]+", " ", norm[:m.start()]).strip()
            bench = re.sub(r"^(?:on|the)\s+", "", bench)
            return bench, key
    return norm, ""


def is_metric_table(header: list[str], rows: list[list[str]]) -> bool:
    """是否「实验指标对比表」：表头带指标，或前两列的行标签带指标。"""
    if sum(1 for h in (header or [])[1:] if split_metric(h)[1]) >= 2:
        return True
    hits = sum(1 for r in rows or [] if any(split_metric(c)[1] for c in (r or [])[:2]))
    return hits >= 2


# ---------------------------------------------------------------- 本文方法识别
def method_tokens(item=None) -> list[str]:
    """从标题里取方法代号（「CVPR 2025 | SFD：…」→ SFD），供标签匹配兜底。"""
    title = str(getattr(item, "title", "") or "")
    tail = re.split(r"[：:]", title, maxsplit=1)[0]
    tail = re.sub(r"^.*?[|｜]", "", tail)
    tokens: list[str] = []
    for raw in re.split(r"[\s,，、/]+", tail):
        tok = raw.strip(" .,:：;；()（）[]【】-—")
        if len(tok) < 2 or tok.lower() in _STOP_TOKENS:
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", tok):
            continue
        if tok.lower() not in (t.lower() for t in tokens):
            tokens.append(tok)
    extra = str((getattr(item, "extra", None) or {}).get("method_name") or "").strip()
    if extra and extra.lower() not in (t.lower() for t in tokens):
        tokens.append(extra)
    return tokens


def ours_pattern(item=None) -> re.Pattern:
    """本文方法的标签匹配式：ours/proposed/本文 + 标题里的方法代号。"""
    parts = [r"ours", r"our\s+method", r"proposed", r"本文", r"本方法", r"我们的", r"本工作"]
    for tok in method_tokens(item):
        parts.append(re.escape(tok))
    return re.compile("|".join(parts), re.I)


_ABLATION_RE = re.compile(r"w/o|without|\bw\.\s*/|\bw/|ablation|变体|去掉|移除", re.I)


def label_is_ours(text: str, item=None) -> bool:
    """行名/列名是不是「本文方法」。方法代号需要整词命中，避免 SFD 匹配到 SFD-xxx 之外的词。

    消融变体（「SFD w/o TG-D」「w/. Feat-D」）不算本文完整方法：把主方法的数值
    回填到变体行，等于篡改消融结论，比留白更严重。
    """
    raw = str(text or "")
    if not raw.strip() or _ABLATION_RE.search(raw):
        return False
    if re.search(r"ours|our\s+method|proposed|本文|本方法|我们的", raw, re.I):
        return True
    for tok in method_tokens(item):
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", raw):
            return True
    return False


def ours_gap(header: list[str], rows: list[list[str]], item=None, *,
             require: bool = False) -> dict | None:
    """检测表格里的「本文方法缺口」。

    - blank_row：有 Ours 行，但整行没有一个数据（SFD 事故形态）
    - blank_col：有 Ours 列（转置表），但整列没有一个数据（HTML 解析左移的典型结果）
    - missing：require=True（实验结果节的对比表）时，表里根本没有本文方法
    返回 {"kind", "index", "axis"}；无缺口返回 None。
    """
    header = list(header or [])
    rows = [list(r or []) for r in rows or []]
    if not header or not rows:
        return None
    for i, row in enumerate(rows):
        if row and label_is_ours(row[0], item):
            if all(is_blank(c) for c in row[1:]):
                return {"kind": "blank_row", "index": i, "axis": "row"}
    for j, h in enumerate(header):
        if j > 0 and label_is_ours(h, item):
            if all(is_blank(r[j]) for r in rows if j < len(r)):
                return {"kind": "blank_col", "index": j, "axis": "col"}
    if require and not any(label_is_ours(r[0], item) for r in rows if r) \
            and not any(label_is_ours(h, item) for h in header[1:]):
        if sum(1 for r in rows if r and any(not is_blank(c) for c in r[1:])) >= 2:
            return {"kind": "missing", "index": -1, "axis": ""}
    return None


# ---------------------------------------------------------------- 原文表索引
def _label_cols(header: list[str], rows: list[list[str]]) -> int:
    """方法在列方向时，左侧标签列有几列（Benchmark + Metric 两列 / 只有一列）。"""
    if len(header) < 3:
        return 1
    second = [r[1] for r in rows if len(r) > 1]
    texty = sum(1 for v in second if str(v).strip() and not _NUM_ONLY_RE.match(str(v).strip()))
    if second and texty * 2 > len(second):
        return 2      # 第二列是「PIPAL. Trad.SR」这类子集名，不是数值
    head = clean_label(header[1])
    if head in ("metric", "metrics", "指标", "measures"):
        return 2
    return 1


def parse_material(table: dict) -> dict | None:
    """把一张原文表解析成「(基准, 指标) → {方法: 值}」的查表结构。

    支持两种排版：方法在列（表头是方法名，行是基准/指标）与方法在行
    （首列是方法名，表头是「基准 + 指标」）。解析不出两行数据就返回 None。
    """
    header = [str(h or "").strip() for h in (table.get("header") or [])]
    rows = [[str(c or "").strip() for c in (r or [])] for r in (table.get("rows") or [])]
    width = len(header)
    if width < 2 or len(rows) < 2:
        return None
    rows = [(r + [""] * width)[:width] for r in rows]
    head_metrics = [j for j in range(1, width) if split_metric(header[j])[1]]
    row_metric_rows = sum(1 for r in rows if any(split_metric(c)[1] for c in r[:2]))

    entries: dict[tuple[str, str], dict[str, str]] = {}
    by_metric: dict[str, list[tuple[str, str]]] = {}
    methods: list[str] = []

    if len(head_metrics) >= 2 and len(head_metrics) >= row_metric_rows:
        # 方法在行：首列方法名，表头逐列是「基准 + 指标」
        cols: list[tuple[int, str, str]] = []
        bench_ctx = ""
        for j in range(1, width):
            bench, metric = split_metric(header[j])
            if metric:
                cols.append((j, bench or bench_ctx, metric))
            elif clean_label(header[j]):
                bench_ctx = clean_label(header[j])
        if len(cols) < 1:
            return None
        for row in rows:
            if not row or not row[0] or is_blank(row[0]):
                continue
            method = clean_label(row[0])
            if not method:
                continue
            if method not in methods:
                methods.append(method)
            for j, bench, metric in cols:
                value = _num_of(row[j]) if j < len(row) else ""
                if not value:
                    continue
                entries.setdefault((bench, metric), {})[method] = value
                by_metric.setdefault(metric, []).append((bench, method, value))
    else:
        # 方法在列：左侧 1–2 列是基准/指标，后面每列一个方法
        label_cols = _label_cols(header, rows)
        methods = [clean_label(h) for h in header[label_cols:]]
        methods = [m for m in methods if m]
        if not methods:
            return None
        bench_carry = ""
        for row in rows:
            labels = row[:label_cols]
            bench, metric = "", ""
            for cell in labels:
                b, m = split_metric(cell)
                if m:
                    metric = m
                    bench = b or bench or bench_carry
                elif clean_label(cell):
                    bench = bench or clean_label(cell)
            bench = bench or bench_carry
            if bench:
                bench_carry = bench
            if not metric:
                continue
            for k, method in enumerate(methods):
                cell = row[label_cols + k] if label_cols + k < len(row) else ""
                value = _num_of(cell)
                if not value:
                    continue
                entries.setdefault((bench, metric), {})[method] = value
                by_metric.setdefault(metric, []).append((bench, method, value))

    if len(entries) < 2:
        return None
    return {
        "caption": str(table.get("caption") or ""),
        "methods": methods,
        "entries": entries,
        "by_metric": by_metric,
        "header": header,
        "rows": rows,
        "ours": [m for m in methods if label_is_ours(m)],
    }


def build_index(tables: list[dict], item=None) -> list[dict]:
    """把原文表解析成索引；解析错位（Ours 列整列空）的表不许进索引。"""
    out = []
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        header = [str(h or "") for h in (t.get("header") or [])]
        rows = [[str(c or "") for c in (r or [])] for r in (t.get("rows") or [])]
        gap = ours_gap(header, rows, item)
        if gap and gap["kind"] in ("blank_row", "blank_col"):
            continue          # 表头有 Ours、列里没数 = 解析左移，回填会张冠李戴
        parsed = parse_material(t)
        if parsed:
            out.append(parsed)
    return out


# ---------------------------------------------------------------- 回填
def _lookup(idx: dict, bench: str, metric: str, method: str) -> str:
    """按 (基准, 指标) 取某方法的数值；基准写法有出入时只在**同一方法**内兜底。

    绝不允许「指标唯一值」式的兜底跨方法取值——SFD 事故里就是它把 RankSR 的
    29.65 填进了 Ours 行，数据看着完整、其实是张冠李戴，比留白更糟。
    """
    hit = (idx["entries"].get((bench, metric)) or {}).get(method)
    if hit:
        return hit
    if bench:
        for (b, m), vals in idx["entries"].items():
            if m == metric and (b.startswith(bench) or bench.startswith(b)) and method in vals:
                return vals[method]
    pairs = [(b, v) for b, meth, v in idx["by_metric"].get(metric, [])
             if meth == method and (not bench or b == bench)]
    values = {v for _b, v in pairs}
    return values.pop() if len(values) == 1 else ""


def _pick_method(idx: dict, rows: list[list[str]], item) -> str:
    """挑出这张原文表里代表本文的方法名；多个 Ours 变体时取最左（作者的主动手）。"""
    ours = idx.get("ours") or []
    if not ours:
        return ""
    labels = [clean_label(r[0]) for r in rows if r]
    for name in ours:
        if name in labels or any(name in lab or lab in name for lab in labels if lab):
            return name
    return ours[0]


def _score_match(idx: dict, header: list[str], rows: list[list[str]]) -> int:
    """目标表与原文表的吻合度：同一格数值对上的加分，对不上的扣分。"""
    score = 0
    for row in rows:
        label = clean_label(row[0]) if row else ""
        if not label or label_is_ours(label):
            continue
        method = next((m for m in idx["methods"] if m == label or label in m or m in label), "")
        if not method:
            continue
        for j in range(1, min(len(header), len(row))):
            bench, metric = split_metric(header[j])
            if not metric or is_blank(row[j]):
                continue
            want = _lookup(idx, bench, metric, method)
            if not want:
                continue
            score += 2 if _num_of(row[j]) == want else -2
    return score


def repair_block(block: dict, tables: list[dict], item=None) -> dict | None:
    """把一张残缺表按原文表重建：只保留能逐格回源的数值，Ours 行必在场。

    返回新的 table 块；无法重建（原文里也没有本文方法数值）返回 None。
    """
    header = [str(h or "") for h in (block.get("header") or [])]
    rows = [[str(c or "") for c in (r or [])] for r in (block.get("rows") or [])]
    gap = ours_gap(header, rows, item)
    if not gap:
        return None
    indexes = build_index(tables, item)
    if not indexes:
        return None
    scored = [(_score_match(idx, header, rows), -i, idx) for i, idx in enumerate(indexes)]
    scored.sort(reverse=True)
    best = scored[0]
    idx = best[2] if best[0] > 0 else next((x[2] for x in scored if x[2]["ours"]), None)
    if not idx or not idx["ours"]:
        return None
    method = _pick_method(idx, rows, item)
    if not method:
        return None

    # 列：沿用原表的指标列（读者看到的口径不变），最多 4 个；解析不出的列丢弃
    cols: list[tuple[str, str]] = []
    names: list[str] = []
    for j in range(1, len(header)):
        bench, metric = split_metric(header[j])
        if not metric or (bench, metric) in cols:
            continue
        cols.append((bench, metric))
        names.append(header[j].strip() or f"{bench} {metric.upper()}".strip())
        if len(cols) >= 4:
            break
    ours_values = [_lookup(idx, b, m, method) for b, m in cols]
    if not cols or any(not v for v in ours_values):
        return None

    # 行：原有对手行里能逐格回源的全部保留（顺序不变），Ours 行放最后
    new_rows: list[list[str]] = []
    for row in rows:
        if not row or is_blank(row[0]) or label_is_ours(row[0], item):
            continue
        label = clean_label(row[0])
        other = next((m for m in idx["methods"]
                      if m == label or label in m or m in label), "")
        if not other:
            continue
        values = [_lookup(idx, b, m, other) for b, m in cols]
        if any(not v for v in values):
            continue
        new_rows.append([row[0].strip()] + values)
    ours_label = next((row[0].strip() for row in rows
                       if row and label_is_ours(row[0], item)), "") or "Ours"
    # 「+Ours」这种泛称升级成原文里的具体方法名（RRDB +Ours），读者才知道是哪一版
    pretty = _pretty_method(idx, method)
    if not re.search(r"[A-Za-z]{2,}", ours_label.replace("Ours", "").replace("ours", "")):
        ours_label = pretty
    new_rows.append([ours_label] + ours_values)
    if len(new_rows) < 2:
        return None
    return {"type": "table", "header": ["方法"] + names, "rows": new_rows}


def _pretty_method(idx: dict, method: str) -> str:
    """查表里的方法名还原成原文写法（保留大小写与 +Ours 后缀）。"""
    label_cols = _label_cols(idx["header"], idx["rows"])
    for j in range(label_cols, len(idx["header"])):
        if clean_label(idx["header"][j]) == method:
            return idx["header"][j].strip()
    for row in idx["rows"]:
        if row and clean_label(row[0]) == method:
            return row[0].strip()
    return method


def prose_fallback(item=None) -> dict:
    """无数据可回填时的文字交代：点名原文表格，不编数字、不留空表。"""
    caption = ""
    for t in (getattr(item, "extra", None) or {}).get("tables") or []:
        cap = str((t or {}).get("caption") or "")
        m = re.search(r"\bTable\s*(\d+[a-z]?)", cap, re.I)
        if m:
            caption = f"Table {m.group(1)}"
            break
    where = f"原文 {caption}" if caption else "原文表格"
    return {
        "type": "p",
        "text": f"作者在{where}中报告了完整的定量对比；本文重点讨论其方法设计与"
                f"感知—失真权衡，具体指标以原文表格为准。",
    }


# ---------------------------------------------------------------- 成稿侧
def repair_spec_tables(spec: dict, item=None, log=None) -> int:
    """成稿兜底：把 spec 里所有「Ours 留白」的表按原文重建；建不出来就撤表换文字。

    返回处理的表数。只动有缺口的表——正常的表逐字不动。
    """
    sections = spec.get("sections") if isinstance(spec, dict) else None
    if not isinstance(sections, list):
        return 0
    tables = [t for t in ((getattr(item, "extra", None) or {}).get("tables") or [])
              if isinstance(t, dict)]
    fixed = 0
    out: list[dict] = []
    for block in sections:
        if not isinstance(block, dict) or block.get("type") != "table":
            out.append(block)
            continue
        gap = ours_gap(block.get("header") or [], block.get("rows") or [], item)
        if not gap:
            out.append(block)
            continue
        new = repair_block(block, tables, item)
        if new:
            out.append(new)
            if log:
                log(f"实验表格已按原文回填：{gap['kind']} → "
                    f"{len(new['rows'])} 行 × {len(new['header'])} 列（Ours 有数）")
        else:
            out.append(prose_fallback(item))
            if log:
                log(f"实验表格 {gap['kind']} 且原文无可回填数值 → 已撤表改文字说明")
        fixed += 1
    if fixed:
        spec["sections"] = out
    return fixed


# ---------------------------------------------------------------- Markdown 侧（编辑器保存）
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")


def _md_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    line = "| " + " | ".join(str(h) for h in header) + " |"
    sep = "|" + "|".join("---" for _ in header) + "|"
    body = ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return [line, sep] + body


def repair_markdown_tables(markdown: str, item=None, log=None, *,
                           drop_unfixable: bool = True) -> tuple[str, int]:
    """在 Markdown 层做同样的回填/撤表，保证编辑器保存的正文与排版一致。

    drop_unfixable=False 用于编辑器保存：补不了时保留原表、由保存闸门报错提示
    人工处理——静默删掉用户刚写的表，比报错更糟。
    """
    lines = str(markdown or "").splitlines()
    tables = [t for t in ((getattr(item, "extra", None) or {}).get("tables") or [])
              if isinstance(t, dict)]
    out: list[str] = []
    i = 0
    fixed = 0
    while i < len(lines):
        if not _TABLE_LINE_RE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        start = i
        while i < len(lines) and _TABLE_LINE_RE.match(lines[i]):
            i += 1
        block_lines = lines[start:i]
        if len(block_lines) < 2:
            out.extend(block_lines)
            continue
        header = _md_cells(block_lines[0])
        body = [_md_cells(x) for x in block_lines[2:]] if len(block_lines) > 2 else []
        gap = ours_gap(header, body, item)
        if not gap:
            out.extend(block_lines)
            continue
        block = {"type": "table", "header": header, "rows": body}
        new = repair_block(block, tables, item)
        if new:
            out.extend(_md_table(new["header"], new["rows"]))
            if log:
                log(f"表格已按原文回填（{gap['kind']}）：Ours 行数据完整")
        elif drop_unfixable:
            note = prose_fallback(item)
            out.extend(["", note["text"], ""])
            if log:
                log("表格 Ours 行无数据且原文无可回填 → 已撤表改文字说明")
        else:
            out.extend(block_lines)      # 保留原表，交给保存闸门报错
            if log:
                log("表格 Ours 行无数据且原文无可回填 → 保留原表待人工处理")
        fixed += 1
    text = "\n".join(out)
    if str(markdown or "").endswith("\n"):
        text += "\n"
    return text, fixed


def has_ours_data(tables: list[dict], item=None) -> bool:
    """素材里是否至少有一张「本文方法有数」的表（成稿前的素材断言）。"""
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        header = [str(h or "") for h in (t.get("header") or [])]
        rows = [[str(c or "") for c in (r or [])] for r in (t.get("rows") or [])]
        if ours_gap(header, rows, item):
            continue
        if any(label_is_ours(h, item) for h in header[1:]) \
                or any(label_is_ours(r[0], item) for r in rows if r):
            return True
    return False


def usable_tables(tables: list[dict], item=None) -> list[dict]:
    """过滤掉「表头写着 Ours、列里全空」的错位表，别让模型照抄成残缺表。"""
    out = []
    for t in tables or []:
        if not isinstance(t, dict):
            continue
        header = [str(h or "") for h in (t.get("header") or [])]
        rows = [[str(c or "") for c in (r or [])] for r in (t.get("rows") or [])]
        gap = ours_gap(header, rows, item)
        if gap and gap["kind"] in ("blank_row", "blank_col"):
            continue
        out.append(t)
    return out
