"""修复存量稿件里「错位」的实验表格：用原 PDF 重新解析出来的表替换掉错表。

背景：早期 ``pdf_tables`` 会把跨列分组表头拆成多列、把下方柱状图的轴刻度吸进
表体，成稿里就留下一张 13 列的错位表（数据还在，但列对不上指标）。解析器已
重构，本脚本负责把**历史出稿**里那类表按原表重建：

    python -m scripts.fix_table_issue --issue paper-20261107-8d38f8 \\
        --pdf out/uploads/xxx.pdf --match DENSE-HAZE --drop Year

只动表格块：正文、图片、引用一律逐字保留（人工意图至上），替换后先过
``edit.assert_tables_stable`` 守卫与「数值原样在表里」校验，再走 ``save_edit``
落库（rev 自增、重排 HTML）。原稿备份在 ``out/_table_backup/``。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from app import db, edit
from app.config import settings
from app.render.md_parse import parse_markdown
from app.sources.paper_pdf import pdf_tables

_ROW_PREFIX = re.compile(r"^\s*\|")
# 表格里的数值：允许负数/小数/百分号，用来做「原样照抄」的逐格比对
_NUM_OK = re.compile(r"^-?\d+(?:\.\d+)?%?$").match


def _split_cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def _table_blocks(markdown: str) -> list[dict]:
    """把 md 里每张表定位成 {start, end, header, rows}（行号为 0 基）。"""
    lines = markdown.splitlines()
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        if not _ROW_PREFIX.match(lines[i]):
            i += 1
            continue
        start = i
        raw: list[list[str]] = []
        while i < len(lines) and _ROW_PREFIX.match(lines[i]):
            raw.append(_split_cells(lines[i]))
            i += 1
        # 第二行是 |---|---| 分隔行
        if len(raw) >= 2 and all(re.fullmatch(r":?-{2,}:?", c) for c in raw[1] if c):
            body = [r for r in raw[2:] if any(c for c in r)]
            blocks.append({"start": start, "end": i, "header": raw[0], "rows": body})
        else:
            blocks.append({"start": start, "end": i, "header": [], "rows": raw})
    return blocks


def _hole_ratio(header: list[str], rows: list[list[str]]) -> float:
    cells = [c for c in header] + [c for r in rows for c in r]
    return sum(1 for c in cells if not c) / max(1, len(cells))


def _md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |",
           "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def _norm_name(cell: str) -> str:
    """方法名归一：「KDMG (VGG19) [2]」→「k dmg vgg19」，只用来对行，不写回。"""
    text = re.sub(r"\[[^\]]*\]", " ", str(cell or ""))
    text = re.sub(r"\(([^)]*)\)", r" \1 ", text)
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _pick_table(tables: list[dict], match: str, index: int) -> dict | None:
    if index:
        return tables[index - 1] if 0 < index <= len(tables) else None
    needle = (match or "").strip().lower()
    for t in tables:
        if needle and needle in (t.get("caption") or "").lower():
            return t
    return None


def _drop_columns(header: list[str], rows: list[list[str]], drops: list[str]) -> tuple:
    keep = [j for j, h in enumerate(header)
            if not any(d and d.lower() in str(h).lower() for d in drops)]
    return ([header[j] for j in keep],
            [[(r[j] if j < len(r) else "") for j in keep] for r in rows],
            keep)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="用原 PDF 的正确表替换存量稿里的错位表格")
    ap.add_argument("--issue", required=True, help="issue_id，如 paper-20261107-8d38f8")
    ap.add_argument("--pdf", required=True, help="原始本地 PDF 路径")
    ap.add_argument("--match", default="", help="按表注关键字选表，如 DENSE-HAZE")
    ap.add_argument("--index", type=int, default=0, help="按序号选表（1 起），优先于 --match")
    ap.add_argument("--drop", action="append", default=[], help="整列裁剪，可多次，如 --drop Year")
    ap.add_argument("--block", type=int, default=0,
                    help="指定替换第几张表（1 起）；0 = 自动找空洞率最高的错表")
    ap.add_argument("--dry", action="store_true", help="只打印将要发生的改动，不落库")
    args = ap.parse_args(argv)

    article = db.get_article(args.issue)
    if not article or not article.markdown:
        print(f"✗ 找不到稿件或稿内无 markdown：{args.issue}")
        return 1

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"✗ PDF 不存在：{pdf_path}")
        return 1

    tables = pdf_tables(pdf_path.read_bytes(), limit=12)
    if not tables:
        print("✗ 未能从 PDF 解析出任何表格")
        return 1
    print("PDF 表格：")
    for i, t in enumerate(tables, 1):
        print(f"  {i}. {len(t['header'])} 列 × {len(t['rows'])} 行  {t['caption'][:64]}")

    source = _pick_table(tables, args.match, args.index)
    if not source:
        print("✗ 没选中任何表，请用 --match/--index 指定")
        return 1

    header, rows, keep = _drop_columns(source["header"], source["rows"], args.drop)
    new_block = _md_table(header, rows)

    blocks = [b for b in _table_blocks(article.markdown) if b["header"]]
    if not blocks:
        print("✗ 稿内没有可替换的表格块")
        return 1
    if args.block:
        if not 0 < args.block <= len(blocks):
            print(f"✗ --block 越界：稿内共 {len(blocks)} 张表")
            return 1
        target = blocks[args.block - 1]
    else:
        target = max(blocks, key=lambda b: _hole_ratio(b["header"], b["rows"]))
    print(f"\n选中稿内表：第 {blocks.index(target) + 1}/{len(blocks)} 张，"
          f"{len(target['header'])} 列 × {len(target['rows'])} 行，"
          f"空洞率 {_hole_ratio(target['header'], target['rows']):.0%}")

    lines = article.markdown.splitlines()
    old_block = lines[target["start"]:target["end"]]
    new_lines = lines[:target["start"]] + new_block + lines[target["end"]:]
    new_md = "\n".join(new_lines)
    if article.markdown.endswith("\n"):
        new_md += "\n"

    # —— 数据契约校验：以 PDF 原表为准，逐格断言，残渣只许丢不许留 ——
    def _nums(row: list[str]) -> set[str]:
        return {c.strip() for c in row if _NUM_OK(c)}

    pdf_nums = {n for r in source["rows"] for n in _nums(r)}
    new_nums = {n for r in rows for n in _nums(r)}
    invented = sorted(new_nums - pdf_nums)
    if invented:
        print(f"✗ 新表出现 PDF 里没有的数值：{invented}")
        return 1

    # 逐行逐格：裁列后 PDF 行与新表行必须一模一样（这就是「零错位」的机器断言）
    src_rows = [[(r[j] if j < len(r) else "") for j in keep] for r in source["rows"]]
    for a, b in zip(src_rows, rows):
        if a != b:
            print(f"✗ 逐格不一致：PDF {a} ≠ 新表 {b}")
            return 1

    # 原稿表里被丢掉的数值：必须全部是图表残渣（PDF 里压根没有的数）
    old_nums = {c.strip() for r in target["rows"] for c in r if _NUM_OK(c)}
    residue = sorted(old_nums - pdf_nums)
    kept_old = sorted(old_nums & pdf_nums)
    print(f"\n原表数值 {len(old_nums)} 个：{len(kept_old)} 个来自 PDF 原表，"
          f"{len(residue)} 个是图表残渣（柱状图轴刻度/图例），将被清除")
    if residue:
        print("  残渣样例：", residue[:10])

    # —— 行序与 PDF 逐行对齐校验 ——
    want = [_norm_name(r[0]) for r in source["rows"]]
    got = [_norm_name(r[0]) for r in rows]
    if want != got:
        print("✗ 行序与 PDF 不一致：")
        print("   PDF:", want)
        print("   新表:", got)
        return 1

    print("\n--- 旧表 ---")
    print("\n".join(old_block))
    print("--- 新表 ---")
    print("\n".join(new_block))

    spec, figures = parse_markdown(new_md)
    try:
        edit.assert_tables_stable(spec, figures)
    except ValueError as exc:
        print(f"✗ 表格守卫未通过：{exc}")
        return 1

    changed = edit.changed_scope(article.markdown, new_md)
    print(f"\n改动范围：{changed['changed_blocks']}/{changed['total_blocks']} 块，"
          f"表格改动 {changed['tables_changed']}（共 {changed['total_tables']} 张）")

    if args.dry:
        print("\n（--dry：未落库）")
        return 0

    backup = settings.out_dir / "_table_backup"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / f"{article.issue_id}.md").write_text(article.markdown, encoding="utf-8")

    payload = edit.save_edit(article, new_md)
    print(f"\n✅ 已修复 {article.issue_id}：{len(header)} 列 × {len(rows)} 行，"
          f"rev={payload.get('rev')}，原稿备份 out/_table_backup/{article.issue_id}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
