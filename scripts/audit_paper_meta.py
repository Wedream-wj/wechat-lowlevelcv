"""历史出稿·论文稿的元信息一致性体检（可选自动修复）。

背景：2026-11-14《NEURIPS 2026 | DeLang-SR》一期，信息卡的论文名称与作者来自
同批另一篇论文（MUTE）。流水线侧已加原子绑定（`app/filters/grounding.py`），
但**已经入库的历史稿**仍带着错位信息，需要一次全量体检。

跑法：

    python -m scripts.audit_paper_meta            # 只报告
    python -m scripts.audit_paper_meta --fix      # 按正文主体重绑并重排入库（rev+1）

判据与流水线完全一致：正文配图来源 / 方法代号 / 信息卡标题相似度。
修复只改信息卡三行文字，不动正文任何一句话。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, edit                                   # noqa: E402
from app.config import settings                             # noqa: E402
from app.filters import grounding                          # noqa: E402
from app.render.md_parse import parse_markdown              # noqa: E402
from app.spec_tools import paper_meta_values                # noqa: E402

_ROW_RE = {label: re.compile(rf"^(- \*\*{label}\*\*：).*$", re.M)
           for label in ("论文名称", "发表期刊/会议", "作者团队")}


def _rewrite_rows(markdown: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """只替换信息卡里的三行文字；缺行不补（避免改动正文结构），交由人工确认。"""
    out, touched = markdown, []
    for label, rx in _ROW_RE.items():
        truth = (values.get(label) or "").strip()
        if not truth or not rx.search(out):
            continue
        new = rx.sub(lambda m, v=truth: m.group(1) + v, out, count=1)
        if new != out:
            out = new
            touched.append(label)
    return out, touched


def _fill_authors(target, log=print) -> None:
    """候选条目没带作者时（Awesome 清单类源），按标题回查一次学术数据库补全。

    只补「作者」这一个字段，标题/venue 仍以目标条目为准；查不到就保持为空，
    由 `bind_paper_meta` 删掉作者行——宁可少一行，绝不拿别篇论文的作者顶上。
    """
    if getattr(target, "authors", None):
        return
    try:
        from app.sources import base, paper_search

        async def _go():
            async with base.HttpClient() as client:
                return await paper_search.search_paper(client, target.title, timeout=45)

        hit = asyncio.run(_go())
        rec = hit.record
        if rec and grounding._similarity(rec.title, target.title) >= 0.72 and rec.authors:
            target.authors = rec.authors
            log(f"  · 作者已按标题回查补全：{len(rec.authors)} 位（{rec.authors[0]} 等）")
    except Exception as exc:      # 断网/限流不该阻断修稿
        log(f"  · 作者回查失败（{type(exc).__name__}），作者行将按要求删除")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="重绑并保存（rev+1）")
    ap.add_argument("--limit", type=int, default=500)
    args = ap.parse_args()

    db.init_db()
    rows = db.search_articles(module="paper", page=1, page_size=args.limit)["items"]
    print(f"体检 {len(rows)} 期论文稿（判据：正文配图 / 方法代号 / 信息卡标题）")
    bad = fixed = 0
    for row in rows:
        article = db.get_article(row["issue_id"])
        if not article or not article.markdown:
            continue
        spec, _ = parse_markdown(article.markdown)
        target, info = grounding.resolve_meta_subject(spec, article.items)
        if not info.get("mismatch"):
            continue
        bad += 1
        print(f"\n· {row['issue_id']}  {(article.title or '')[:44]}")
        print(f"  原因：{info['reason']}")
        print(f"  目标：{info['target'][:70]}")
        if not args.fix or target is None:
            continue
        _fill_authors(target)
        values = paper_meta_values(target)
        new_md, touched = _rewrite_rows(article.markdown, values)
        if not touched:
            print("  ⚠ 信息卡行格式无法定位，跳过（请人工处理）")
            continue
        # 复核：改写后再判一次，必须不再 mismatch
        spec2, _ = parse_markdown(new_md)
        _, info2 = grounding.resolve_meta_subject(spec2, article.items)
        if info2.get("mismatch"):
            print("  ⚠ 改写后仍不一致，跳过（请人工处理）")
            continue
        backup = settings.out_dir / "_meta_backup"
        backup.mkdir(parents=True, exist_ok=True)
        (backup / f"{article.issue_id}.md").write_text(article.markdown, encoding="utf-8")
        edit.save_edit(article, new_md)
        meta = db.edit_meta(article.issue_id)
        fixed += 1
        print(f"  ✅ 已修复 {touched}，rev={meta.get('rev', '?')}，"
              f"状态 {meta.get('status', '?')}（正文未改动，原稿备份在 out/_meta_backup/）")

    print(f"\n结果：{bad} 期存在元信息错位" + (f"，已修复 {fixed} 期" if args.fix else "（--fix 可自动修复）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
