"""历史出稿「实验表格 Ours 零留白」体检与修复。

事故原型：《CVPR 2025 | SFD》的「关键实验结果解读」表格里，本文方法一行写着
`+Ours | — | — | — | —`——表头明明是 `+Ours`（方法**列**），解析时 rowspan 没展开、
右侧数值整体左移，模型照抄列名补了一行破折号。学术界里，没有本文方法数据的
对比表等于自证「本文没有结果」，公信力直接归零。

处理规则（写死）：
1. 表里有 Ours 行但整行空 → 用原文表逐格回填（基准 + 指标定位，数值逐字照抄）；
2. 表里是 Ours 列但整列空 → 同上；
3. 原文素材里也取不到 → **撤掉整张表**，改成一句「完整定量对比见原文 Table N」，
   绝不保留带空行的残缺表。

用法：
    python -m scripts.audit_ours_tables                # 只体检，打印问题清单
    python -m scripts.audit_ours_tables --fix          # 体检 + 落库修复（自动备份）
    python -m scripts.audit_ours_tables --offline      # 不联网：只用稿内已有素材
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from app import db, edit
from app.config import settings
from app.render.md_parse import parse_markdown
from app.sources import arxiv as arxiv_source
from app.sources import paper_pdf as paper_pdf_source
from app.sources.base import HttpClient
from app import tables_guard

BACKUP_DIR = settings.out_dir / "_ours_table_backup"


def _paper_item(article):
    return next((i for i in (article.items or [])
                 if getattr(i, "module", "") == "paper"), None)


def _gap_tables(markdown: str, item=None) -> list[dict]:
    """稿内所有「Ours 留白」的表（返回 {header, rows, gap} 列表）。"""
    spec, _ = parse_markdown(markdown or "\n")
    out = []
    for block in spec.get("sections") or []:
        if not isinstance(block, dict) or block.get("type") != "table":
            continue
        gap = tables_guard.ours_gap(block.get("header") or [], block.get("rows") or [], item)
        if gap:
            out.append({"header": block.get("header") or [],
                        "rows": block.get("rows") or [], "gap": gap})
    return out


async def _fetch_tables(client: HttpClient, item) -> list[dict]:
    """二级来源：arXiv HTML（全宽抽表）→ PDF 词坐标重建。"""
    tables: list[dict] = []
    arxiv_id = paper_pdf_source._arxiv_id_of(item)
    if arxiv_id:
        try:
            html = await asyncio.wait_for(
                client.text(f"https://arxiv.org/html/{arxiv_id}"), timeout=40)
            tables = arxiv_source.extract_tables(html, limit=6, max_cols=14)
        except Exception as exc:
            print(f"    · arXiv HTML 解析失败：{type(exc).__name__}")
    if not tables or not tables_guard.has_ours_data(tables, item):
        try:
            pdf_bytes, _attempts = await paper_pdf_source.acquire_pdf(client, item)
            if pdf_bytes:
                tables = tables + paper_pdf_source.pdf_tables(pdf_bytes, limit=8)
        except Exception as exc:
            print(f"    · PDF 重解析失败：{type(exc).__name__}")
    return tables


async def main(fix: bool, offline: bool) -> int:
    rows = db.search_articles(module="paper", page=1, page_size=500)["items"]
    problems = 0
    fixed = 0
    async with HttpClient() as client:
        for row in rows:
            article = db.get_article(row["issue_id"])
            if not article or not article.markdown:
                continue
            item = _paper_item(article)
            gaps = _gap_tables(article.markdown, item)
            if not gaps:
                continue
            problems += 1
            print(f"\n## {article.issue_id} | {article.title}")
            for g in gaps:
                print(f"   · {g['gap']['kind']} | 表头 {g['header']}")
            if not fix:
                continue
            if item is None:
                print("   · 无论文素材，跳过（请人工处理）")
                continue
            tables = [t for t in ((item.extra or {}).get("tables") or []) if isinstance(t, dict)]
            if not tables_guard.has_ours_data(tables_guard.usable_tables(tables, item), item) \
                    and not offline:
                print("   · 稿内素材缺本文方法数值，拉取 arXiv HTML / PDF 二级解析…")
                fresh = await _fetch_tables(client, item)
                if fresh:
                    item.extra["tables"] = paper_pdf_source._dedupe_tables(tables + fresh)
            new_md, changed = tables_guard.repair_markdown_tables(
                article.markdown, item, log=lambda m: print(f"   · {m}"))
            if not changed:
                print("   · 未能自动修复（原文也取不到数值），请人工确认")
                continue
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            (BACKUP_DIR / f"{article.issue_id}.md").write_text(
                article.markdown, encoding="utf-8")
            try:
                edit.save_edit(article, new_md)
            except ValueError as exc:
                print(f"   · 保存被闸门拒绝：{exc}")
                continue
            fixed += 1
            print("   · 已修复并落库")
    print(f"\n体检 {len(rows)} 期论文稿：{problems} 期有 Ours 留白"
          + (f"，已修复 {fixed} 期" if fix else "（--fix 可自动修复）"))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="实验表格 Ours 零留白体检")
    parser.add_argument("--fix", action="store_true", help="体检并落库修复（自动备份）")
    parser.add_argument("--offline", action="store_true", help="不联网，只用稿内素材")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.fix, args.offline)))
